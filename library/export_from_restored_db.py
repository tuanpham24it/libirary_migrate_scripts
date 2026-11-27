# Script XUẤT dữ liệu Thư viện từ database OpenEduCat (Odoo 18, các module
# openeducat_library + openeducat_digital_library + openeducat_library_marc21
# bản đã customize) đã được pg_restore vào một database Postgres MỚI trên máy
# này. Cùng cách làm với các script export trong
# odoo_19_community/migration_scripts: đọc trực tiếp bằng SQL, không cần
# cài/chạy code OpenEduCat.
#
# ĐÍCH: module eaut_library (library_student + library) trên Odoo 19. Đây
# KHÔNG phải nâng cấp module mà là chuyển sang CẤU TRÚC KHÁC:
#   - op.media (sách cứng) + op.digital.library.material (bản online)
#       => GỘP thành 1 library.material
#   - op.media.unit                => library.material.unit (GIỮ NGUYÊN barcode)
#   - op.student / op.faculty      => library.member (chỉ người có thẻ / từng mượn,
#                                     trừ khi bật IMPORT_ALL_STUDENTS)
#   - op.library.card(.type)       => library.card(.type)
#   - op.media.movement            => library.loan (+ library.fine nếu có tiền phạt)
#
# PHẠM VI ĐÃ CHỐT:
#   - Chuyển: sách (tài liệu, đơn vị tài liệu, file PDF/audio, ảnh bìa), độc giả,
#     thẻ, lịch sử mượn trả, tiền phạt trả muộn.
#   - BỎ: bản online dạng ePub, các bản online THỪA khi 1 sách cứng có nhiều bản
#     online (chỉ giữ 1 bản), media queue, purchase request, ghi danh/đánh giá
#     tài liệu số, chatter cũ.
#   Mọi thứ bị bỏ / bất thường đều được ghi vào file REPORT_PATH để thủ thư rà soát.
#
# CHUẨN BỊ TRƯỚC KHI CHẠY:
#   1. pg_restore bản backup DB educat vào 1 database MỚI (không đè DB đang dùng).
#   2. Copy thư mục filestore của DB educat (filestore/<ten_db_educat>) về máy
#      này, sửa FILESTORE_ROOT. File PDF/audio/ảnh bìa nằm ở đây.
#   3. Sửa các thông số DB_* bên dưới.
#
# CÁCH CHẠY (không cần odoo-bin, python thường trong venv có psycopg2):
#   <venv>\Scripts\python.exe migrate_scripts\library\export_from_restored_db.py
#   (venv nào cũng được, chỉ cần có psycopg2 - vd venv của Odoo 19)
#
# JSON chỉ lưu ĐƯỜNG DẪN file trên đĩa, không nhúng base64 (giống helpdesk),
# script import sẽ đọc từng file khi tạo bản ghi.

import argparse
import configparser
import json
import os
import re
from collections import defaultdict

import psycopg2

# ===== SỬA CÁC THÔNG SỐ NÀY CHO ĐÚNG =====
# DB_HOST/DB_PASSWORD de RONG = dung Postgres "peer" auth qua unix socket.
DB_HOST = ''
DB_PORT = 5432
DB_NAME = 'odoo_educat_backup_migrate'   # database da pg_restore DB educat vao
DB_USER = 'odoo_mis'
DB_PASSWORD = ''

FILESTORE_ROOT = '/opt/odoo_mis_community_v19/migration_data/library/educat_filestore'
OUTPUT_PATH = '/opt/odoo_mis_community_v19/migration_data/library/library_export.json'
REPORT_PATH = '/opt/odoo_mis_community_v19/migration_data/library/library_export_report.txt'
DB_DATAS_DUMP_DIR = '/opt/odoo_mis_community_v19/migration_data/library/db_datas_dump'

# True = xuất TẤT CẢ sinh viên thành độc giả (kể cả chưa có thẻ, chưa mượn).
# False = chỉ sinh viên/giảng viên có thẻ thư viện hoặc từng có phiếu mượn.
IMPORT_ALL_STUDENTS = False
# Đích không có "loại tài liệu" (op.media.type) - đưa tên loại thành 1 thẻ phân loại.
MEDIA_TYPE_AS_TAG = True
# ==========================================

# Loại bản số được giữ và tên tương ứng bên đích. ePub KHÔNG có trong danh sách
# => bị bỏ (đã chốt).
KEEP_DIGITAL_TYPES = {'pdf': 'pdf', 'audiobook': 'audio'}

AUDIO_EXT_BY_MIME = {
    'audio/mpeg': '.mp3', 'audio/mp3': '.mp3', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
    'audio/aac': '.aac', 'audio/wav': '.wav', 'audio/x-wav': '.wav', 'audio/ogg': '.ogg',
}

# Trường MARC21 / mô tả có cùng tên ở op.media, op.digital.library.material và
# library.material.
COMMON_FIELDS = [
    'price', 'ddc', 'cutter_number', 'cataloging_agency_code', 'language_code',
    'country_code', 'subtitle', 'volume_name', 'volume_number', 'publication_place',
    'publish_year', 'num_pages', 'size',
]
MEDIA_COLUMNS = ['name', 'isbn', 'edition', 'internal_code', 'description', 'active',
                 'category_id', 'industry_id', 'media_type_id'] + COMMON_FIELDS
DIGITAL_COLUMNS = ['name', 'isbn_code', 'material_edition', 'material_description',
                   'material_source', 'publish_online', 'material_type', 'category_id',
                   'industry_id', 'language_id', 'media_id'] + COMMON_FIELDS

warnings = defaultdict(list)   # nhom -> [dong canh bao]


def warn(group, line):
    warnings[group].append(line)


def as_text(value, prefer_langs=('vi_VN', 'en_US')):
    """Field translate=True lưu dạng jsonb {lang: text}; quy về 1 chuỗi."""
    if isinstance(value, dict):
        for lang in prefer_langs:
            if value.get(lang):
                return value[lang]
        for v in value.values():
            if v:
                return v
        return ''
    return value or ''


def clean(value):
    """Chuỗi đã strip, hoặc None nếu rỗng."""
    value = as_text(value)
    if isinstance(value, str):
        value = value.strip()
    return value or None


def existing_columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {row[0] for row in cur.fetchall()}


def table_exists(cur, table):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,))
    return cur.fetchone() is not None


def select_rows(cur, table, wanted_columns, order='id'):
    """SELECT id + các cột có thật trong bảng. Trả về list dict. Cột không tồn
    tại được báo 1 lần và coi như None."""
    if not table_exists(cur, table):
        warn('Bang khong ton tai', table)
        return []
    available = existing_columns(cur, table)
    cols = [c for c in wanted_columns if c in available]
    missing = [c for c in wanted_columns if c not in available]
    if missing:
        print("CANH BAO: bang %s khong co cot: %s (bo qua)" % (table, ', '.join(missing)))
    cur.execute("SELECT id%s FROM %s ORDER BY %s" % (''.join(', ' + c for c in cols), table, order))
    rows = []
    for row in cur.fetchall():
        values = {c: None for c in wanted_columns}
        values.update(dict(zip(cols, row[1:])))
        values['id'] = row[0]
        rows.append(values)
    return rows


def names_by_id(cur, table):
    if not table_exists(cur, table):
        return {}
    cur.execute("SELECT id, name FROM %s" % table)
    return {row[0]: clean(row[1]) for row in cur.fetchall()}


def read_m2m(cur, candidates):
    """Đọc quan hệ many2many {id_nguon: [id_dich,...]} từ MỌI bảng ứng viên có
    thật (gộp lại). Cần nhiều ứng viên vì bản customize đã đổi tên bảng quan hệ
    (vd tác giả của tài liệu số: bản gốc dùng tên mặc định, bản custom khai báo
    relation='op_digital_material_author_rel') - dữ liệu nhập trước khi đổi vẫn
    nằm ở bảng cũ."""
    result = defaultdict(list)
    for table, src_col, dst_col in candidates:
        if not table_exists(cur, table):
            continue
        cols = existing_columns(cur, table)
        if src_col not in cols or dst_col not in cols:
            continue
        cur.execute("SELECT %s, %s FROM %s" % (src_col, dst_col, table))
        for src, dst in cur.fetchall():
            if dst not in result[src]:
                result[src].append(dst)
    return result


def fetch_field_files(cur, res_model, res_field, res_ids):
    """File của 1 field Binary/Image (attachment=True): {res_id: {name, path,
    mimetype}}. Chỉ lấy đường dẫn, không đọc nội dung."""
    result = {}
    if not res_ids:
        return result
    cur.execute("""
        SELECT DISTINCT ON (res_id) id, res_id, name, store_fname, mimetype, (db_datas IS NOT NULL)
        FROM ir_attachment
        WHERE res_model = %s AND res_field = %s AND res_id = ANY(%s)
        ORDER BY res_id, id DESC
    """, (res_model, res_field, list(res_ids)))
    rows = cur.fetchall()
    for att_id, res_id, name, store_fname, mimetype, has_db in rows:
        path = None
        if has_db:
            os.makedirs(DB_DATAS_DUMP_DIR, exist_ok=True)
            cur.execute("SELECT db_datas FROM ir_attachment WHERE id = %s", (att_id,))
            path = os.path.join(DB_DATAS_DUMP_DIR, str(att_id))
            with open(path, 'wb') as f:
                f.write(bytes(cur.fetchone()[0]))
        elif store_fname:
            candidate = os.path.join(FILESTORE_ROOT, store_fname)
            if os.path.exists(candidate):
                path = candidate
            else:
                warn('File khong tim thay trong filestore',
                     '%s #%s (%s): %s' % (res_model, res_id, res_field, candidate))
        result[res_id] = {'name': name, 'path': path, 'mimetype': mimetype or ''}
    return result


def merge_names(*lists):
    """Gộp nhiều danh sách tên, bỏ trùng không phân biệt hoa thường, giữ thứ tự."""
    seen, out = set(), []
    for names in lists:
        for name in names or []:
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    return out


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', ' ', name or '').strip()
    return (name or 'tai_lieu')[:150]


def parse_args():
    """Tham số dòng lệnh GHI ĐÈ các hằng số ở đầu file (khỏi phải sửa code khi
    chạy test / production). --odoo-conf: lấy db_host/db_port/db_user/
    db_password từ file conf Odoo (vd của server Odoo 18 đang chạy DB nguồn)."""
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    global FILESTORE_ROOT, OUTPUT_PATH, REPORT_PATH, DB_DATAS_DUMP_DIR, IMPORT_ALL_STUDENTS
    parser = argparse.ArgumentParser(description='Xuat du lieu thu vien OpenEduCat ra JSON.')
    parser.add_argument('--odoo-conf', help='Doc thong tin ket noi DB tu file conf Odoo')
    parser.add_argument('--db', help='Ten database nguon (OpenEduCat da restore)')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--user')
    parser.add_argument('--filestore', help='Thu muc filestore cua DB nguon (chua cac thu muc 00..ff)')
    parser.add_argument('--output', help='Thu muc ghi library_export.json + bao cao')
    parser.add_argument('--all-students', action='store_true', help='Xuat tat ca sinh vien thanh doc gia')
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
    if args.output:
        OUTPUT_PATH = os.path.join(args.output, 'library_export.json')
        REPORT_PATH = os.path.join(args.output, 'library_export_report.txt')
        DB_DATAS_DUMP_DIR = os.path.join(args.output, 'db_datas_dump')
    IMPORT_ALL_STUDENTS = IMPORT_ALL_STUDENTS or args.all_students
    if not os.path.isdir(FILESTORE_ROOT):
        raise SystemExit("Khong tim thay FILESTORE_ROOT: %s" % FILESTORE_ROOT)


def main():
    parse_args()
    print("Nguon: database '%s' @ %s, filestore: %s" % (DB_NAME, DB_HOST or 'socket', FILESTORE_ROOT))
    conn_kwargs = {'dbname': DB_NAME, 'user': DB_USER, 'port': DB_PORT}
    if DB_HOST:
        conn_kwargs['host'] = DB_HOST
    if DB_PASSWORD:
        conn_kwargs['password'] = DB_PASSWORD
    conn = psycopg2.connect(**conn_kwargs)
    conn.set_session(readonly=True)
    cur = conn.cursor()

    # ================= 1. Cấu hình =================
    industries = []
    for row in select_rows(cur, 'op_industry', ['name', 'code']):
        industries.append({'source_id': row['id'], 'name': clean(row['name']) or 'Nganh %s' % row['id'],
                           'code': clean(row['code'])})

    categories = []
    for row in select_rows(cur, 'op_digital_library_category', ['name', 'parent_id', 'category_code']):
        if not clean(row['category_code']):
            warn('Danh muc thieu ma (ky hieu xep gia se thieu phan nay)',
                 '#%s %s' % (row['id'], clean(row['name'])))
        categories.append({'source_id': row['id'], 'name': clean(row['name']) or 'Danh muc %s' % row['id'],
                           'code': clean(row['category_code']), 'parent_source_id': row['parent_id']})

    media_type_names = names_by_id(cur, 'op_media_type')
    author_names = names_by_id(cur, 'op_author')
    publisher_names = names_by_id(cur, 'op_publisher')
    tag_names = names_by_id(cur, 'op_tag')
    corporate_names = names_by_id(cur, 'op_corporate_author')
    d_author_names = names_by_id(cur, 'op_digital_library_author')
    d_publisher_names = names_by_id(cur, 'op_digital_library_publisher')
    lang_codes = {}
    cur.execute("SELECT id, code FROM res_lang")
    for lid, code in cur.fetchall():
        lang_codes[lid] = code

    # ================= 2. Sách cứng =================
    medias = select_rows(cur, 'op_media', MEDIA_COLUMNS)
    media_by_id = {m['id']: m for m in medias}
    m_authors = read_m2m(cur, [('op_media_author_rel', 'media_id', 'author_id')])
    m_publishers = read_m2m(cur, [('op_media_publisher_rel', 'media_id', 'publisher_id')])
    m_corporate = read_m2m(cur, [('op_media_corporate_author_rel', 'media_id', 'corporate_author_id')])
    m_additional = read_m2m(cur, [('op_media_additional_author_rel', 'media_id', 'author_id')])
    m_tags = read_m2m(cur, [('op_media_op_tag_rel', 'op_media_id', 'op_tag_id')])
    print("Sach cung (op.media): %d" % len(medias))

    # ================= 3. Bản online =================
    digitals = select_rows(cur, 'op_digital_library_material', DIGITAL_COLUMNS)
    digital_by_id = {d['id']: d for d in digitals}
    d_authors = read_m2m(cur, [
        ('op_digital_material_author_rel', 'material_id', 'author_id'),
        ('op_digital_library_author_op_digital_library_material_rel',
         'op_digital_library_material_id', 'op_digital_library_author_id'),
    ])
    d_publishers = read_m2m(cur, [
        ('op_digital_material_publisher_rel', 'material_id', 'publisher_id'),
        ('op_digital_library_material_op_digital_library_publisher_rel',
         'op_digital_library_material_id', 'op_digital_library_publisher_id'),
    ])
    d_corporate = read_m2m(cur, [('op_digital_material_corporate_author_rel', 'material_id', 'corporate_author_id')])
    d_additional = read_m2m(cur, [('op_digital_material_additional_author_rel', 'material_id', 'author_id')])
    digital_ids = list(digital_by_id)
    data_files = fetch_field_files(cur, 'op.digital.library.material', 'material_data', digital_ids)
    cover_files = fetch_field_files(cur, 'op.digital.library.material', 'material_cover', digital_ids)
    print("Ban online (op.digital.library.material): %d" % len(digitals))

    # ----- Liên kết sách cứng <-> bản online (2 nguồn: bảng M2M + cột media_id) -----
    links = defaultdict(list)   # media_id -> [digital_id]
    for media_id, d_ids in read_m2m(cur, [('op_media_digital_material_rel', 'media_id', 'digital_material_id')]).items():
        for d_id in d_ids:
            if media_id in media_by_id and d_id in digital_by_id and d_id not in links[media_id]:
                links[media_id].append(d_id)
    for d in digitals:
        if d.get('media_id') in media_by_id and d['id'] not in links[d['media_id']]:
            links[d['media_id']].append(d['id'])

    def digital_rank(d):
        # Ưu tiên: loại được giữ, đang publish, có file, id nhỏ
        return (d['material_type'] not in KEEP_DIGITAL_TYPES, not d['publish_online'],
                not (data_files.get(d['id']) or {}).get('path'), d['id'])

    used_digital = set()
    primary_digital = {}   # media_id -> digital_id
    for media_id in sorted(links):
        candidates = [digital_by_id[i] for i in links[media_id] if i not in used_digital]
        candidates.sort(key=digital_rank)
        chosen = None
        for d in candidates:
            if chosen is None and d['material_type'] in KEEP_DIGITAL_TYPES:
                chosen = d
                continue
            reason = 'ePub' if d['material_type'] not in KEEP_DIGITAL_TYPES else 'ban online thua'
            warn('Ban online bi bo (%s)' % reason,
                 'digital #%s "%s" (sach cung #%s "%s")' % (
                     d['id'], clean(d['name']), media_id, clean(media_by_id[media_id]['name'])))
            used_digital.add(d['id'])
        if chosen:
            primary_digital[media_id] = chosen['id']
            used_digital.add(chosen['id'])
        for i in links[media_id]:
            used_digital.add(i)
    linked_to_many = defaultdict(list)
    for media_id, d_ids in links.items():
        for d_id in d_ids:
            linked_to_many[d_id].append(media_id)
    for d_id, media_ids in linked_to_many.items():
        if len(media_ids) > 1:
            warn('Ban online lien ket nhieu sach cung (chi gop vao 1 sach)',
                 'digital #%s lien ket sach cung %s' % (d_id, media_ids))

    # ================= 4. Đơn vị tài liệu =================
    units = select_rows(cur, 'op_media_unit', ['name', 'media_id', 'barcode', 'state', 'active'])
    units_by_media = defaultdict(list)
    for u in units:
        units_by_media[u['media_id']].append(u)
    print("Don vi tai lieu (op.media.unit): %d" % len(units))

    # ================= 5. Thẻ, độc giả, phiếu mượn =================
    card_types = []
    for row in select_rows(cur, 'op_library_card_type', ['name', 'allow_media', 'duration', 'penalty_amt_per_day']):
        max_loans, loan_days = row['allow_media'] or 0, row['duration'] or 0
        if max_loans <= 0 or loan_days <= 0:
            warn('Loai the co gia tri <= 0 (dich bat buoc > 0, da doi thanh 1)',
                 '#%s %s: so tai lieu=%s, so ngay=%s' % (row['id'], clean(row['name']), max_loans, loan_days))
        card_types.append({
            'source_id': row['id'], 'name': clean(row['name']) or 'Loai the %s' % row['id'],
            'max_loans': max(1, max_loans), 'loan_days': max(1, loan_days),
            'penalty_per_day': row['penalty_amt_per_day'] or 0.0,
        })

    cards_raw = select_rows(cur, 'op_library_card',
                            ['number', 'type', 'student_id', 'faculty_id', 'library_card_type_id',
                             'issue_date', 'active'])
    movements = select_rows(cur, 'op_media_movement',
                            ['media_id', 'media_unit_id', 'type', 'student_id', 'faculty_id',
                             'library_card_id', 'issued_date', 'return_date', 'actual_return_date',
                             'penalty', 'state', 'invoice_id', 'active'],
                            order='issued_date, id')

    def card_member_key(card):
        if card['type'] == 'faculty' and card['faculty_id']:
            return 'faculty_%s' % card['faculty_id']
        if card['student_id']:
            return 'student_%s' % card['student_id']
        if card['faculty_id']:
            return 'faculty_%s' % card['faculty_id']
        return None

    card_by_id = {c['id']: c for c in cards_raw}
    needed_students, needed_faculty = set(), set()
    for c in cards_raw:
        key = card_member_key(c)
        if key and key.startswith('student_'):
            needed_students.add(c['student_id'])
        elif key:
            needed_faculty.add(c['faculty_id'])
    for m in movements:
        if m['student_id']:
            needed_students.add(m['student_id'])
        if m['faculty_id']:
            needed_faculty.add(m['faculty_id'])
    if IMPORT_ALL_STUDENTS and table_exists(cur, 'op_student'):
        cur.execute("SELECT id FROM op_student")
        needed_students |= {r[0] for r in cur.fetchall()}

    # Số thẻ theo độc giả (giảng viên không có mã => dùng số thẻ làm mã độc giả)
    card_numbers_by_member = defaultdict(list)
    for c in sorted(cards_raw, key=lambda c: (not c['active'], c['id'])):
        key = card_member_key(c)
        if key and clean(c['number']):
            card_numbers_by_member[key].append(clean(c['number']))

    partner_cols = existing_columns(cur, 'res_partner')
    phone_expr = 'COALESCE(NULLIF(p.phone, \'\'), p.mobile)' if 'mobile' in partner_cols else 'p.phone'
    gender_map = {'m': 'male', 'male': 'male', 'f': 'female', 'female': 'female', 'o': 'other', 'other': 'other'}

    members = []
    member_keys = set()

    # ----- Sinh viên -----
    if needed_students:
        cur.execute("""
            SELECT s.id, s.gr_no, s.active, s.gender, s.birth_date, s.id_number,
                   p.name, p.email, %s, p.street
            FROM op_student s JOIN res_partner p ON p.id = s.partner_id
            WHERE s.id = ANY(%%s)
        """ % phone_expr, (list(needed_students),))
        student_rows = cur.fetchall()

        # Lớp / ngành / khoa / niên khoá lấy từ khoá học gần nhất (ưu tiên đang học)
        course_info = {}
        sc_cols = existing_columns(cur, 'op_student_course')
        if sc_cols:
            order_running = "(sc.state = 'running') DESC, " if 'state' in sc_cols else ''
            ay_join, ay_sel = '', 'NULL'
            if 'academic_years_id' in sc_cols and table_exists(cur, 'op_academic_year'):
                ay_join, ay_sel = 'LEFT JOIN op_academic_year ay ON ay.id = sc.academic_years_id', 'ay.name'
            cur.execute("""
                SELECT DISTINCT ON (sc.student_id) sc.student_id, b.name, c.name, d.name, %s
                FROM op_student_course sc
                LEFT JOIN op_batch b ON b.id = sc.batch_id
                LEFT JOIN op_course c ON c.id = sc.course_id
                LEFT JOIN op_department d ON d.id = c.department_id
                %s
                WHERE sc.student_id = ANY(%%s)
                ORDER BY sc.student_id, %s sc.id DESC
            """ % (ay_sel, ay_join, order_running), (list(needed_students),))
            for sid, batch, course, dept, ay in cur.fetchall():
                course_info[sid] = {'class_name': clean(batch), 'major': clean(course),
                                    'department': clean(dept), 'academic_year': clean(ay)}

        for sid, gr_no, active, gender, birth, id_number, name, email, phone, street in student_rows:
            key = 'student_%s' % sid
            code = clean(gr_no)
            if not code:
                code = (card_numbers_by_member.get(key) or ['SV%s' % sid])[0]
                warn('Sinh vien thieu ma sinh vien (gr_no) - da dung so the / SV<id>',
                     '#%s %s -> ma %s' % (sid, clean(name), code))
            email = (clean(email) or '').lower() or None
            if not email:
                warn('Doc gia thieu email (import se bo qua, kem the + phieu muon)',
                     'sinh vien #%s %s (%s)' % (sid, clean(name), code))
            members.append({
                'source_key': key, 'member_type': 'student', 'code': code,
                'name': clean(name) or code, 'email': email, 'phone': clean(phone),
                'gender': gender_map.get(gender), 'birthday': str(birth) if birth else None,
                'id_number': clean(id_number), 'address': clean(street),
                'source_active': bool(active),
                **course_info.get(sid, {}),
            })
            member_keys.add(key)

    # ----- Giảng viên -----
    if needed_faculty:
        fac_cols = existing_columns(cur, 'op_faculty')
        dept_sel = 'd.name' if 'main_department_id' in fac_cols else 'NULL'
        dept_join = 'LEFT JOIN op_department d ON d.id = f.main_department_id' if 'main_department_id' in fac_cols else ''
        emp_sel, emp_join = 'NULL', ''
        if 'emp_id' in fac_cols and table_exists(cur, 'hr_employee'):
            emp_sel, emp_join = 'e.work_email', 'LEFT JOIN hr_employee e ON e.id = f.emp_id'
        cur.execute("""
            SELECT f.id, f.active, f.gender, f.birth_date, f.id_number,
                   p.name, p.email, %s, p.street, %s, %s
            FROM op_faculty f JOIN res_partner p ON p.id = f.partner_id
            %s %s
            WHERE f.id = ANY(%%s)
        """ % (phone_expr, dept_sel, emp_sel, dept_join, emp_join), (list(needed_faculty),))
        for fid, active, gender, birth, id_number, name, email, phone, street, dept, work_email in cur.fetchall():
            key = 'faculty_%s' % fid
            code = (card_numbers_by_member.get(key) or ['GV%s' % fid])[0]
            email = ((clean(email) or clean(work_email)) or '').lower() or None
            if not email:
                warn('Doc gia thieu email (import se bo qua, kem the + phieu muon)',
                     'giang vien #%s %s (%s)' % (fid, clean(name), code))
            members.append({
                'source_key': key, 'member_type': 'faculty', 'code': code,
                'name': clean(name) or code, 'email': email, 'phone': clean(phone),
                'gender': gender_map.get(gender), 'birthday': str(birth) if birth else None,
                'id_number': clean(id_number), 'address': clean(street),
                'department': clean(dept), 'source_active': bool(active),
            })
            member_keys.add(key)
    print("Doc gia: %d (sinh vien %d, giang vien %d)" % (
        len(members), len(needed_students), len(needed_faculty)))

    # ----- Thẻ: mỗi độc giả chỉ 1 thẻ "đang dùng" (ràng buộc bên đích) -----
    cards = []
    has_active_card = set()
    for c in sorted(cards_raw, key=lambda c: (not c['active'], -c['id'])):
        key = card_member_key(c)
        if not key or key not in member_keys:
            warn('The khong xac dinh duoc doc gia (bo qua)', 'the #%s so %s' % (c['id'], clean(c['number'])))
            continue
        state = 'active' if c['active'] else 'cancelled'
        if state == 'active' and key in has_active_card:
            state = 'cancelled'
            warn('Doc gia co nhieu the dang dung (giu the moi nhat, the cu => Da huy)',
                 'the #%s so %s cua %s' % (c['id'], clean(c['number']), key))
        if state == 'active':
            has_active_card.add(key)
        cards.append({
            'source_id': c['id'], 'number': clean(c['number']), 'member_key': key,
            'card_type_source_id': c['library_card_type_id'],
            'issue_date': str(c['issue_date']) if c['issue_date'] else None,
            'state': state, 'active': bool(c['active']),
        })

    # ----- Phiếu mượn -----
    invoice_info = {}
    invoice_ids = {m['invoice_id'] for m in movements if m['invoice_id']}
    if invoice_ids:
        cur.execute("SELECT id, payment_state, invoice_date FROM account_move WHERE id = ANY(%s)",
                    (list(invoice_ids),))
        invoice_info = {r[0]: {'payment_state': r[1], 'invoice_date': r[2]} for r in cur.fetchall()}

    state_map = {'issue': 'issue', 'return': 'returned', 'return_done': 'returned', 'lost': 'lost'}
    unit_ids_known = {u['id'] for u in units}
    loans = []
    skipped_states = defaultdict(int)
    for m in movements:
        if m['state'] not in state_map:
            skipped_states[m['state']] += 1
            continue
        card = card_by_id.get(m['library_card_id'])
        member_key = card_member_key(card) if card else None
        if not member_key:
            if m['student_id']:
                member_key = 'student_%s' % m['student_id']
            elif m['faculty_id']:
                member_key = 'faculty_%s' % m['faculty_id']
        if member_key not in member_keys:
            warn('Phieu muon khong xac dinh duoc doc gia (bo qua)', 'movement #%s' % m['id'])
            continue
        if m['media_unit_id'] not in unit_ids_known:
            warn('Phieu muon khong co don vi tai lieu (bo qua)', 'movement #%s' % m['id'])
            continue
        if not m['issued_date']:
            warn('Phieu muon thieu ngay muon (bo qua)', 'movement #%s' % m['id'])
            continue
        issue_date = m['issued_date']
        due_date = m['return_date'] or issue_date
        if due_date < issue_date:
            warn('Phieu muon co han tra truoc ngay muon (da dat han tra = ngay muon)', 'movement #%s' % m['id'])
            due_date = issue_date
        state = state_map[m['state']]
        actual = m['actual_return_date']
        if actual and actual < issue_date:
            actual = issue_date

        fine = None
        penalty = m['penalty'] or 0.0
        if penalty > 0 and state == 'returned':
            inv = invoice_info.get(m['invoice_id']) or {}
            paid = m['state'] == 'return_done' or inv.get('payment_state') in ('paid', 'in_payment')
            paid_date = inv.get('invoice_date') or actual if paid else None
            fine = {'amount': penalty, 'state': 'paid' if paid else 'unpaid',
                    'paid_date': str(paid_date) if paid_date else None}
        loans.append({
            'source_id': m['id'], 'member_key': member_key,
            'card_source_id': card['id'] if card else None,
            'unit_source_id': m['media_unit_id'],
            'issue_date': str(issue_date), 'due_date': str(due_date),
            'return_date': str(actual) if actual and state != 'issue' else None,
            'late_days': max(0, (actual - due_date).days) if actual and state == 'returned' else 0,
            'state': state, 'active': m['active'] is not False, 'fine': fine,
        })
    for st, n in skipped_states.items():
        warn('Phieu muon o trang thai khong chuyen (available/reserve)', '%s: %d phieu' % (st, n))

    # Mỗi đơn vị tài liệu chỉ được có 1 phiếu "đang mượn" (ràng buộc bên đích)
    open_by_unit = defaultdict(list)
    for loan in loans:
        if loan['state'] == 'issue':
            open_by_unit[loan['unit_source_id']].append(loan)
    for unit_id, open_loans in open_by_unit.items():
        for loan in open_loans[:-1]:   # đã sắp theo ngày mượn => giữ phiếu MỚI NHẤT
            loan['state'] = 'returned'
            warn('Don vi tai lieu co nhieu phieu dang muon (giu phieu moi nhat, phieu cu => Da tra)',
                 'unit #%s: movement #%s' % (unit_id, loan['source_id']))

    # Trạng thái cuối của đơn vị tài liệu suy ra từ phiếu mượn
    last_loan_by_unit = {}
    for loan in loans:
        last_loan_by_unit[loan['unit_source_id']] = loan
    unit_final_state = {}
    for u in units:
        last = last_loan_by_unit.get(u['id'])
        if u['id'] in open_by_unit:
            state = 'issue'
        elif last and last['state'] == 'lost':
            state = 'lost'
        else:
            state = 'available'
        if (u['state'] == 'issue') != (state == 'issue'):
            warn('Trang thai don vi tai lieu lech voi phieu muon (da theo phieu muon)',
                 'unit #%s barcode %s: cu=%s, moi=%s' % (u['id'], u['barcode'], u['state'], state))
        unit_final_state[u['id']] = state

    # ================= 6. Gộp sách cứng + bản online thành tài liệu =================
    def digital_part(d):
        f = data_files.get(d['id']) or {}
        if not f.get('path'):
            warn('Ban online khong co file (chuyen thong tin, khong co file)',
                 'digital #%s "%s"' % (d['id'], clean(d['name'])))
            return None
        dtype = KEEP_DIGITAL_TYPES[d['material_type']]
        if dtype == 'pdf':
            ext = '.pdf'
        else:
            ext = AUDIO_EXT_BY_MIME.get(f['mimetype']) or os.path.splitext(f.get('name') or '')[1] or '.mp3'
        return {
            'type': dtype, 'file': f['path'],
            'filename': safe_filename(clean(d['name'])) + ext,
            'source': clean(d['material_source']),
            'publish_online': bool(d['publish_online']),
        }

    def build_material(media, digital):
        entry = {f: None for f in ['name', 'isbn', 'edition', 'internal_code'] + COMMON_FIELDS}
        if digital:
            entry.update({f: digital[f] for f in COMMON_FIELDS})
            entry.update({'name': clean(digital['name']), 'isbn': clean(digital['isbn_code']),
                          'edition': clean(digital['material_edition'])})
        if media:
            for f in COMMON_FIELDS:
                if media[f] not in (None, '', 0):
                    entry[f] = media[f]
            for f in ('name', 'isbn', 'edition', 'internal_code'):
                if clean(media[f]):
                    entry[f] = clean(media[f])
        for f in COMMON_FIELDS:
            if isinstance(entry[f], str):
                entry[f] = entry[f].strip() or None
        entry['name'] = entry['name'] or 'Tai lieu khong ten'
        entry['active'] = media['active'] is not False if media else True
        src = media or digital
        entry['category_source_id'] = (media and media['category_id']) or (digital and digital['category_id'])
        entry['industry_source_id'] = (media and media['industry_id']) or (digital and digital['industry_id'])
        entry['language_code'] = lang_codes.get(digital['language_id']) if digital else None

        mid, did = (media or {}).get('id'), (digital or {}).get('id')
        entry['author_names'] = merge_names([author_names.get(i) for i in m_authors.get(mid, [])],
                                            [d_author_names.get(i) for i in d_authors.get(did, [])])
        entry['additional_author_names'] = merge_names(
            [author_names.get(i) for i in m_additional.get(mid, [])],
            [d_author_names.get(i) for i in d_additional.get(did, [])])
        entry['corporate_author_names'] = merge_names(
            [corporate_names.get(i) for i in m_corporate.get(mid, [])],
            [corporate_names.get(i) for i in d_corporate.get(did, [])])
        entry['publisher_names'] = merge_names([publisher_names.get(i) for i in m_publishers.get(mid, [])],
                                               [d_publisher_names.get(i) for i in d_publishers.get(did, [])])
        tags = [tag_names.get(i) for i in m_tags.get(mid, [])]
        if MEDIA_TYPE_AS_TAG and media and media['media_type_id']:
            tags.append(media_type_names.get(media['media_type_id']))
        entry['tag_names'] = merge_names(tags)

        # Mô tả: HTML của bản online nếu có nội dung, không thì text của sách cứng
        html = as_text(digital['material_description']) if digital else ''
        if html and re.sub(r'<[^>]*>|&nbsp;|\s', '', html):
            entry['description_html'] = html
        else:
            entry['description_text'] = clean(media['description']) if media else None

        entry['digital'] = digital_part(digital) if digital else None
        cover = cover_files.get(did) if did else None
        entry['cover_file'] = cover['path'] if cover and cover.get('path') else None

        entry['units'] = [{
            'source_id': u['id'], 'name': clean(u['name']) or entry['name'],
            'barcode': clean(u['barcode']), 'state': unit_final_state[u['id']],
            'active': u['active'] is not False,
        } for u in (units_by_media.get(mid, []) if media else [])]
        if media and media['isbn'] and digital and digital['isbn_code'] and \
                clean(media['isbn']) != clean(digital['isbn_code']):
            warn('ISBN sach cung va ban online khac nhau (lay theo sach cung)',
                 'media #%s %s / digital #%s %s' % (mid, media['isbn'], did, digital['isbn_code']))
        return entry, src

    materials = []
    for media in medias:
        did = primary_digital.get(media['id'])
        entry, _src = build_material(media, digital_by_id.get(did) if did else None)
        entry['source_key'] = 'media_%s' % media['id']
        entry['source_media_id'] = media['id']
        entry['source_digital_id'] = did
        materials.append(entry)
    digital_only = 0
    for d in digitals:
        if d['id'] in used_digital:
            continue
        if d['material_type'] not in KEEP_DIGITAL_TYPES:
            warn('Ban online bi bo (ePub)', 'digital #%s "%s" (khong co sach cung)' % (d['id'], clean(d['name'])))
            continue
        entry, _src = build_material(None, d)
        entry['source_key'] = 'digital_%s' % d['id']
        entry['source_media_id'] = None
        entry['source_digital_id'] = d['id']
        materials.append(entry)
        digital_only += 1
    print("Tai lieu sau khi gop: %d (sach cung %d, trong do co ban online %d; chi ban online %d)" % (
        len(materials), len(medias), len(primary_digital), digital_only))

    # ================= 7. Ghi file =================
    payload = {
        'industries': industries,
        'categories': categories,
        'materials': materials,
        'card_types': card_types,
        'members': members,
        'cards': cards,
        'loans': loans,
        'stats': {
            'media': len(medias), 'digital': len(digitals), 'materials': len(materials),
            'units': sum(len(m['units']) for m in materials),
            'members': len(members), 'cards': len(cards), 'loans': len(loans),
            'open_loans': sum(1 for l in loans if l['state'] == 'issue'),
            'fines': sum(1 for l in loans if l['fine']),
            'unpaid_fine_total': sum(l['fine']['amount'] for l in loans
                                     if l['fine'] and l['fine']['state'] == 'unpaid'),
        },
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, default=str)

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write('BAO CAO EXPORT THU VIEN (educat -> eaut_library)\n\n')
        for k, v in payload['stats'].items():
            f.write('%-20s %s\n' % (k, v))
        for group in sorted(warnings):
            lines = warnings[group]
            f.write('\n=== %s (%d) ===\n' % (group, len(lines)))
            for line in lines:
                f.write('  - %s\n' % line)

    print("\nThong ke:", json.dumps(payload['stats'], ensure_ascii=False))
    for group in sorted(warnings):
        print("CANH BAO [%d] %s" % (len(warnings[group]), group))
    print("\nDa xuat ra: %s\nBao cao chi tiet: %s" % (OUTPUT_PATH, REPORT_PATH))
    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
