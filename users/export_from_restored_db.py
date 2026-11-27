# Script XUẤT tài khoản đăng nhập (internal user + portal user) từ database
# OpenEduCat (Odoo 18) đã pg_restore, KÈM HASH MẬT KHẨU để người dùng đăng
# nhập lại bằng mật khẩu cũ trên Odoo 19, không cần đổi/đặt lại mật khẩu.
# Dựa trên odoo_19_community/migration_scripts/users_contacts (bản MIS).
#
# HASH TƯƠNG THÍCH: Odoo 18 và 19 cùng dùng passlib pbkdf2_sha512
# ("$pbkdf2-sha512$<rounds>$<salt>$<hash>") - copy nguyên chuỗi là đăng nhập được.
#
# KHÁC BẢN MIS: không xuất toàn bộ contact (độc giả + contact đã được script
# library tạo); mỗi tài khoản sinh viên/giảng viên được gắn với độc giả tương
# ứng (source_key giống script library: student_<id> / faculty_<id>) để import
# dùng ĐÚNG contact của độc giả => cổng /my/library nhận ra tài khoản.
#
# LƯU Ý: tài khoản portal nào bên nguồn CHƯA TỪNG đặt mật khẩu (tạo sẵn cho
# sinh viên nhưng sinh viên chưa kích hoạt) thì không có hash để chuyển - vẫn
# tạo tài khoản, người dùng dùng "Quên mật khẩu" để đặt lần đầu.
#
# CẢNH BÁO BẢO MẬT: file JSON có HASH mật khẩu - script đặt quyền chỉ chủ file
# đọc được (Linux); XOÁ file sau khi migrate xong, không gửi/lưu đi nơi khác.
#
# CÁCH CHẠY:
#   <venv>/bin/python migrate_scripts/users/export_from_restored_db.py \
#       --odoo-conf <odoo18.conf> --db <db_nguon> --filestore <filestore> --output <thu_muc_ra>
#   Tuỳ chọn: --include-inactive  (xuất cả tài khoản đã lưu trữ - mặc định chỉ đang hoạt động)

import argparse
import configparser
import json
import os
from collections import defaultdict

import psycopg2

# ===== GIÁ TRỊ MẶC ĐỊNH (ghi đè được bằng tham số dòng lệnh) =====
DB_HOST = ''
DB_PORT = 5432
DB_NAME = 'odoo_educat_backup_migrate'
DB_USER = 'odoo_mis'
DB_PASSWORD = ''
FILESTORE_ROOT = '/opt/odoo_mis_community_v19/migration_data/library/educat_filestore'
OUTPUT_DIR = '/opt/odoo_mis_community_v19/migration_data/users_library'
# ==================================================================

# Tài khoản hệ thống / mẫu - không chuyển
EXCLUDED_LOGINS = {'__system__', 'public', 'portaltemplate', 'default'}


def as_text(value, prefer_langs=('vi_VN', 'en_US')):
    if isinstance(value, dict):
        for lang in prefer_langs:
            if value.get(lang):
                return value[lang]
        for v in value.values():
            if v:
                return v
        return ''
    return value or ''


def existing_columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {row[0] for row in cur.fetchall()}


def table_exists(cur, table):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def parse_args():
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, FILESTORE_ROOT, OUTPUT_DIR
    parser = argparse.ArgumentParser(description='Xuat tai khoan dang nhap (kem hash mat khau) ra JSON.')
    parser.add_argument('--odoo-conf')
    parser.add_argument('--db')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--user')
    parser.add_argument('--filestore', help='Filestore DB nguon (de lay anh dai dien user noi bo)')
    parser.add_argument('--output')
    parser.add_argument('--include-inactive', action='store_true')
    args = parser.parse_args()
    if args.odoo_conf:
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(args.odoo_conf, encoding='utf-8')
        opts = conf['options'] if conf.has_section('options') else {}

        def opt(key):
            value = (opts.get(key) or '').strip()
            return '' if value.lower() in ('', 'false', 'none') else value
        DB_HOST = opt('db_host') or DB_HOST
        DB_PORT = int(opt('db_port') or DB_PORT)
        DB_USER = opt('db_user') or DB_USER
        DB_PASSWORD = opt('db_password') or DB_PASSWORD
    DB_NAME = args.db or DB_NAME
    DB_HOST = args.host if args.host is not None else DB_HOST
    DB_PORT = args.port or DB_PORT
    DB_USER = args.user or DB_USER
    DB_PASSWORD = os.environ.get('PGPASSWORD') or DB_PASSWORD
    FILESTORE_ROOT = args.filestore or FILESTORE_ROOT
    OUTPUT_DIR = args.output or OUTPUT_DIR
    return args.include_inactive


def main():
    include_inactive = parse_args()
    conn_kwargs = {'dbname': DB_NAME, 'user': DB_USER, 'port': DB_PORT}
    if DB_HOST:
        conn_kwargs['host'] = DB_HOST
    if DB_PASSWORD:
        conn_kwargs['password'] = DB_PASSWORD
    conn = psycopg2.connect(**conn_kwargs)
    conn.set_session(readonly=True)
    cur = conn.cursor()

    user_cols = existing_columns(cur, 'res_users')
    partner_cols = existing_columns(cur, 'res_partner')
    tz_sel = 'p.tz' if 'tz' in partner_cols else 'NULL'
    lang_sel = 'p.lang' if 'lang' in partner_cols else 'NULL'
    phone_sel = 'p.phone' if 'phone' in partner_cols else 'NULL'
    sig_sel = 'u.signature' if 'signature' in user_cols else 'NULL'
    cur.execute("""
        SELECT u.id, u.login, u.password, u.share, u.active, u.partner_id,
               p.name, p.email, %s, %s, %s, %s
        FROM res_users u JOIN res_partner p ON p.id = u.partner_id
        WHERE %s
        ORDER BY u.id
    """ % (lang_sel, tz_sel, phone_sel, sig_sel, 'TRUE' if include_inactive else 'u.active'))
    rows = cur.fetchall()

    # ----- Liên kết tài khoản -> độc giả (sinh viên / giảng viên) -----
    member_key_by_user, member_key_by_partner = {}, {}
    if table_exists(cur, 'op_student'):
        s_cols = existing_columns(cur, 'op_student')
        cur.execute("SELECT id, partner_id, %s FROM op_student" % ('user_id' if 'user_id' in s_cols else 'NULL'))
        for sid, partner_id, user_id in cur.fetchall():
            key = 'student_%s' % sid
            if user_id:
                member_key_by_user.setdefault(user_id, key)
            if partner_id:
                member_key_by_partner.setdefault(partner_id, key)
    if table_exists(cur, 'op_faculty'):
        f_cols = existing_columns(cur, 'op_faculty')
        cur.execute("SELECT id, partner_id, %s FROM op_faculty" % ('user_id' if 'user_id' in f_cols else 'NULL'))
        for fid, partner_id, user_id in cur.fetchall():
            key = 'faculty_%s' % fid
            if user_id:
                member_key_by_user.setdefault(user_id, key)
            if partner_id:
                member_key_by_partner.setdefault(partner_id, key)

    # ----- Nhóm quyền (theo XML ID) -----
    groups_by_user = defaultdict(list)
    cur.execute("""
        SELECT r.uid, d.module || '.' || d.name, g.name
        FROM res_groups_users_rel r
        JOIN res_groups g ON g.id = r.gid
        LEFT JOIN ir_model_data d ON d.model = 'res.groups' AND d.res_id = r.gid
    """)
    for uid, xmlid, gname in cur.fetchall():
        groups_by_user[uid].append({'xmlid': xmlid, 'name': as_text(gname)})

    # ----- Ảnh đại diện (chỉ user nội bộ - sinh viên đã có ảnh qua độc giả nếu có) -----
    image_by_partner = {}
    cur.execute("""SELECT res_id, store_fname FROM ir_attachment
                   WHERE res_model = 'res.partner' AND res_field = 'image_1920' AND store_fname IS NOT NULL""")
    for res_id, store_fname in cur.fetchall():
        path = os.path.join(FILESTORE_ROOT, store_fname)
        if os.path.exists(path):
            image_by_partner[res_id] = path

    users, portal_users = [], []
    stats = defaultdict(int)
    for uid, login, password, share, active, partner_id, name, email, lang, tz, phone, signature in rows:
        login = (login or '').strip()
        if not login or login.lower() in EXCLUDED_LOGINS:
            continue
        entry = {
            'source_id': uid, 'login': login, 'name': as_text(name) or login,
            'email': (email or '').strip().lower() or (login.lower() if '@' in login else None),
            'phone': phone or None, 'lang': lang or None, 'tz': tz or None,
            'active': bool(active),
            'password_hash': password or None,
            'member_key': member_key_by_user.get(uid) or member_key_by_partner.get(partner_id),
            'groups': groups_by_user.get(uid, []),
        }
        if share:
            portal_users.append(entry)
            stats['portal'] += 1
            stats['portal_with_password'] += bool(password)
            stats['portal_with_member'] += bool(entry['member_key'])
        else:
            entry['signature'] = as_text(signature) or None
            entry['image_file'] = image_by_partner.get(partner_id)
            users.append(entry)
            stats['internal'] += 1
            stats['internal_with_password'] += bool(password)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, 'users_export.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({'users': users, 'portal_users': portal_users, 'stats': dict(stats)}, f, ensure_ascii=False)
    try:
        os.chmod(output_path, 0o600)   # file co hash mat khau: chi chu file doc duoc (Linux)
    except OSError:
        pass

    print("Internal user: %(internal)d (co mat khau: %(internal_with_password)d)" % dict(stats))
    print("Portal user:   %d (co mat khau: %d, gan duoc doc gia: %d)" % (
        stats['portal'], stats['portal_with_password'], stats['portal_with_member']))
    if stats['portal'] - stats['portal_with_password']:
        print("  -> %d portal user CHUA TUNG dat mat khau ben nguon: van tao tai khoan, nguoi dung "
              "dung 'Quen mat khau' de dat lan dau." % (stats['portal'] - stats['portal_with_password']))
    print("Da xuat ra: %s\nCANH BAO: file chua HASH mat khau - xoa sau khi migrate xong." % output_path)
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
