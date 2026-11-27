# Script XUẤT giao diện Website của thư viện từ database OpenEduCat (Odoo 18)
# đã pg_restore, đọc TRỰC TIẾP bằng SQL. Dựa trên
# odoo_19_community/migration_scripts/website/export_from_restored_db.py (bản
# đã dùng cho MIS), bổ sung cho trường hợp thư viện:
#   - CHỌN website cần chuyển (DB nguồn có nhiều website nháp, chỉ 1 website
#     thật - mặc định lấy website CÓ tên miền).
#   - Logo, favicon, link mạng xã hội, ngôn ngữ mặc định của website.
#   - Bảng màu / font đã tuỳ chỉnh bằng trình soạn web (file SCSS "/_custom/..."
#     + ir.asset riêng của website).
#   - Nội dung view có nhiều bản dịch (arch_db jsonb {lang: xml}): xuất cả bản
#     dịch, script import ghi đúng từng ngôn ngữ.
#
# PHẠM VI: chỉ các ir_ui_view CÓ website_id (trang, header/footer/snippet đã
# sửa riêng cho website), website.page và website.menu của website đã chọn, ảnh
# người dùng upload nhúng trong nội dung trang. KHÔNG chuyển: blog/eLearning
# (dữ liệu nguồn chỉ là demo của Odoo), trang /digital-library (là controller
# của module cũ, thay bằng /my/library của eaut_library - link được đổi ở bước
# import).
#
# CÁCH CHẠY (python thường, cần psycopg2):
#   <venv>\Scripts\python.exe migrate_scripts\website\export_from_restored_db.py ^
#       --odoo-conf <odoo18.conf> --db <db_nguon> --filestore <thu_muc_filestore> --output <thu_muc_ra>
#   Tuỳ chọn: --website-ids 3      (mặc định: các website có tên miền; không có thì tất cả)

import argparse
import configparser
import json
import os
import re

import psycopg2

# ===== GIÁ TRỊ MẶC ĐỊNH (ghi đè được bằng tham số dòng lệnh) =====
DB_HOST = ''
DB_PORT = 5432
DB_NAME = 'odoo_educat_backup_migrate'
DB_USER = 'odoo_mis'
DB_PASSWORD = ''
FILESTORE_ROOT = '/opt/odoo_mis_community_v19/migration_data/library/educat_filestore'
OUTPUT_DIR = '/opt/odoo_mis_community_v19/migration_data/website_library'
# ==================================================================

CANDIDATE_WEBSITE_COLUMNS = [
    'name', 'domain', 'default_lang_id', 'social_facebook', 'social_twitter',
    'social_linkedin', 'social_youtube', 'social_instagram', 'social_github',
    'social_tiktok', 'google_analytics_key',
]
CANDIDATE_VIEW_COLUMNS = ['name', 'key', 'arch_db', 'type', 'active', 'inherit_id',
                          'priority', 'mode', 'website_id']
CANDIDATE_PAGE_COLUMNS = ['view_id', 'url', 'is_published', 'website_indexed', 'website_id']
CANDIDATE_MENU_COLUMNS = ['name', 'url', 'sequence', 'parent_id', 'page_id', 'website_id', 'new_window']

IMAGE_URL_RE = re.compile(r'/web/(?:image|content)/(\d+)')


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


def select(cur, table, wanted, where, params):
    cols = [c for c in wanted if c in existing_columns(cur, table)]
    cur.execute("SELECT id, %s FROM %s WHERE %s ORDER BY id" % (', '.join(cols), table, where), params)
    out = []
    for row in cur.fetchall():
        values = {c: None for c in wanted}
        values.update(dict(zip(cols, row[1:])))
        values['id'] = row[0]
        out.append(values)
    return out


def attachment_file(cur, att_id):
    """(name, mimetype, path) của 1 ir_attachment, chỉ lấy đường dẫn file."""
    cur.execute("SELECT name, mimetype, store_fname, db_datas IS NOT NULL FROM ir_attachment WHERE id = %s",
                (att_id,))
    row = cur.fetchone()
    if not row:
        return None
    name, mimetype, store_fname, has_db = row
    if has_db:
        dump_dir = os.path.join(OUTPUT_DIR, 'db_datas_dump')
        os.makedirs(dump_dir, exist_ok=True)
        cur.execute("SELECT db_datas FROM ir_attachment WHERE id = %s", (att_id,))
        path = os.path.join(dump_dir, str(att_id))
        with open(path, 'wb') as f:
            f.write(bytes(cur.fetchone()[0]))
        return name, mimetype, path
    if store_fname:
        path = os.path.join(FILESTORE_ROOT, store_fname)
        if os.path.exists(path):
            return name, mimetype, path
        print("CANH BAO: khong tim thay file %s (attachment %s)" % (path, att_id))
    return None


def field_file(cur, res_model, res_field, res_id):
    cur.execute("SELECT id FROM ir_attachment WHERE res_model = %s AND res_field = %s AND res_id = %s "
                "ORDER BY id DESC LIMIT 1", (res_model, res_field, res_id))
    row = cur.fetchone()
    found = attachment_file(cur, row[0]) if row else None
    return {'name': found[0], 'mimetype': found[1], 'file': found[2]} if found else None


def parse_args():
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, FILESTORE_ROOT, OUTPUT_DIR
    parser = argparse.ArgumentParser(description='Xuat giao dien website thu vien ra JSON.')
    parser.add_argument('--odoo-conf')
    parser.add_argument('--db')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--user')
    parser.add_argument('--filestore')
    parser.add_argument('--output', help='Thu muc ghi website_export.json')
    parser.add_argument('--website-ids', help='Danh sach id website nguon, vd "3" hoac "1,3"')
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
    if not os.path.isdir(FILESTORE_ROOT):
        raise SystemExit("Khong tim thay FILESTORE_ROOT: %s" % FILESTORE_ROOT)
    return [int(x) for x in args.website_ids.split(',')] if args.website_ids else None


def main():
    wanted_ids = parse_args()
    conn_kwargs = {'dbname': DB_NAME, 'user': DB_USER, 'port': DB_PORT}
    if DB_HOST:
        conn_kwargs['host'] = DB_HOST
    if DB_PASSWORD:
        conn_kwargs['password'] = DB_PASSWORD
    conn = psycopg2.connect(**conn_kwargs)
    conn.set_session(readonly=True)
    cur = conn.cursor()

    if not table_exists(cur, 'website') or not table_exists(cur, 'website_page'):
        raise SystemExit("DB nguon khong co module Website.")

    # ----- Chọn website -----
    all_sites = select(cur, 'website', CANDIDATE_WEBSITE_COLUMNS, 'TRUE', ())
    print("Website trong DB nguon:")
    for s in all_sites:
        print("  #%s %s (ten mien: %s)" % (s['id'], as_text(s['name']), s['domain'] or '-'))
    if wanted_ids:
        sites = [s for s in all_sites if s['id'] in wanted_ids]
    else:
        sites = [s for s in all_sites if s['domain']] or all_sites
    if not sites:
        raise SystemExit("Khong co website nao duoc chon.")
    site_ids = [s['id'] for s in sites]
    print("=> Chuyen website: %s" % ', '.join('#%s' % i for i in site_ids))

    cur.execute("SELECT id, code FROM res_lang")
    lang_codes = dict(cur.fetchall())

    websites_out = []
    for s in sites:
        entry = {
            'source_id': s['id'], 'name': as_text(s['name']) or 'Website %s' % s['id'],
            'domain': s['domain'] or None,
            'default_lang_code': lang_codes.get(s['default_lang_id']),
            'logo': field_file(cur, 'website', 'logo', s['id']),
            'favicon': field_file(cur, 'website', 'favicon', s['id']),
        }
        for f in CANDIDATE_WEBSITE_COLUMNS:
            if f.startswith('social_') or f == 'google_analytics_key':
                entry[f] = s[f] or None
        # Ngôn ngữ đang bật trên website
        if table_exists(cur, 'website_lang_rel'):
            cur.execute("SELECT l.code FROM website_lang_rel r JOIN res_lang l ON l.id = r.lang_id "
                        "WHERE r.website_id = %s", (s['id'],))
            entry['lang_codes'] = [r[0] for r in cur.fetchall()]
        websites_out.append(entry)

    # ----- View riêng của website -----
    cur.execute("SELECT id, key FROM ir_ui_view WHERE key IS NOT NULL")
    view_key_by_id = dict(cur.fetchall())
    view_rows = select(cur, 'ir_ui_view', CANDIDATE_VIEW_COLUMNS, 'website_id = ANY(%s)', (site_ids,))
    views_out = []
    image_cache = {}
    for v in view_rows:
        arch_db = v['arch_db']
        translations = arch_db if isinstance(arch_db, dict) else {'en_US': arch_db or ''}
        translations = {lang: xml for lang, xml in translations.items() if xml}
        if not translations:
            continue
        image_files = {}
        for xml in translations.values():
            for att_id_str in set(IMAGE_URL_RE.findall(xml)):
                if att_id_str not in image_cache:
                    found = attachment_file(cur, int(att_id_str))
                    image_cache[att_id_str] = (
                        {'name': found[0], 'mimetype': found[1], 'file': found[2]} if found else None)
                if image_cache[att_id_str]:
                    image_files[att_id_str] = image_cache[att_id_str]
        views_out.append({
            'source_id': v['id'],
            'name': as_text(v['name']) or 'View %s' % v['id'],
            'key': v['key'] or None,
            'arch_translations': translations,
            'type': v['type'] or 'qweb',
            'active': v['active'] is not False,
            'inherit_source_id': v['inherit_id'],
            'inherit_key': view_key_by_id.get(v['inherit_id']),
            'priority': v['priority'] or 16,
            'mode': v['mode'] or 'primary',
            'website_source_id': v['website_id'],
            'image_files': image_files,
        })

    # ----- Trang + menu -----
    pages_out = [{
        'source_id': p['id'], 'view_source_id': p['view_id'], 'url': p['url'],
        'is_published': bool(p['is_published']),
        'website_indexed': p['website_indexed'] is not False,
        'website_source_id': p['website_id'],
    } for p in select(cur, 'website_page', CANDIDATE_PAGE_COLUMNS, 'website_id = ANY(%s)', (site_ids,))]

    menus_out = [{
        'source_id': m['id'],
        'name': as_text(m['name']) or 'Menu %s' % m['id'],
        'name_translations': m['name'] if isinstance(m['name'], dict) else None,
        'url': m['url'], 'sequence': m['sequence'] or 10,
        'parent_source_id': m['parent_id'], 'page_source_id': m['page_id'],
        'website_source_id': m['website_id'], 'new_window': bool(m['new_window']),
    } for m in select(cur, 'website_menu', CANDIDATE_MENU_COLUMNS, 'website_id = ANY(%s)', (site_ids,))]

    # ----- SCSS tuỳ chỉnh (bảng màu, font...) qua trình soạn web -----
    custom_assets = []
    if table_exists(cur, 'ir_asset') and 'website_id' in existing_columns(cur, 'ir_asset'):
        att_has_site = 'website_id' in existing_columns(cur, 'ir_attachment')
        cur.execute("""SELECT id, website_id, bundle, path, target FROM ir_asset
                       WHERE website_id = ANY(%s) AND path LIKE '/_custom/%%' AND active""", (site_ids,))
        for asset_id, website_id, bundle, path, target in cur.fetchall():
            if att_has_site:
                cur.execute("SELECT id FROM ir_attachment WHERE url = %s AND website_id = %s "
                            "ORDER BY id DESC LIMIT 1", (path, website_id))
            else:
                cur.execute("SELECT id FROM ir_attachment WHERE url = %s ORDER BY id DESC LIMIT 1", (path,))
            row = cur.fetchone()
            found = attachment_file(cur, row[0]) if row else None
            if not found:
                print("CANH BAO: khong tim thay noi dung SCSS tuy chinh %s (website #%s)" % (path, website_id))
                continue
            with open(found[2], 'rb') as f:
                content = f.read().decode('utf-8')
            # bundle tuỳ chỉnh nằm ngay trong URL: /_custom/<bundle>/<duong_dan_goc>
            m = re.match(r'^/_custom/([^/]+)(/.+)$', path)
            custom_assets.append({
                'website_source_id': website_id,
                'url': target or (m.group(2) if m else None),
                'bundle': m.group(1) if m else 'web.assets_frontend',
                'content': content,
            })

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, 'website_export.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({'websites': websites_out, 'views': views_out, 'pages': pages_out,
                   'menus': menus_out, 'custom_assets': custom_assets}, f, ensure_ascii=False)
    print("Da xuat %d website, %d view, %d trang, %d menu, %d anh nhung, %d file SCSS tuy chinh ra: %s" % (
        len(websites_out), len(views_out), len(pages_out), len(menus_out),
        sum(1 for v in image_cache.values() if v), len(custom_assets), output_path))
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
