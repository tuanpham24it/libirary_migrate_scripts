# Script NHAP du lieu Thu vien da xuat boi export_from_restored_db.py vao
# module eaut_library (library_student + library) tren Odoo 19.
#
# CHAY bang Odoo 19 (DB test truoc, DB that sau). eaut_library phai nam trong
# addons_path cua file conf va module 'library' da duoc cai, vd:
#   <odoo19>\venv\Scripts\python.exe <odoo19>\odoo-19.0\odoo-bin -c <odoo19.conf> -d <ten_database> -i library --stop-after-init --no-http
#   <odoo19>\venv\Scripts\python.exe <odoo19>\odoo-19.0\odoo-bin shell -c <odoo19.conf> -d <ten_database> --no-http < migrate_scripts\library\import_to_library.py
#
# AN TOAN KHI CHAY LAI NHIEU LAN: moi ban ghi duoc danh dau bang external id
# (module gia IMPORT_MODULE) theo id goc ben educat. Ban ghi da nhap thi bo
# qua. Cau hinh (tac gia, NXB, the phan loai...) doi chieu theo TEN, nganh theo
# MA, don vi tai lieu theo BARCODE - neu DB dich da co san thi dung lai.
#
# MOI BAN GHI CHAY TRONG 1 SAVEPOINT: 1 ban ghi loi khong lam hong ca lan chay,
# loi duoc liet ke o cuoi. Commit theo tung lo (COMMIT_EVERY) vi tai lieu co
# file PDF lon.
#
# THU TU TAO (theo dung phu thuoc):
#   1. Nganh, danh muc (cha truoc con)
#   2. Tai lieu (gop sach cung + ban online) + don vi tai lieu (giu barcode)
#   3. Loai the, doc gia, the
#   4. Phieu muon (+ khoan phat)
#   5. Doi soat so luong voi file export
#
# PHIEU MUON: library.loan.create() cua module kiem tra quy tac MUON MOI (doc
# gia dang hoat dong, the con han, han muc, don vi tai lieu dang san co...) va
# tu chuyen don vi tai lieu sang "Dang muon" - khong dung duoc cho LICH SU (phieu
# da tra, the da huy...). Script goi THANG lop cha (super(LibraryLoan, ...)) de
# bo qua rieng phan do, van giu mail.thread/ORM va cac rang buoc SQL/constrains
# (vd 1 don vi tai lieu chi 1 phieu dang muon). Trang thai don vi tai lieu da
# duoc tinh san o buoc export va ghi khi tao don vi tai lieu.

import base64
import json
import os

from odoo.tools import plaintext2html
from odoo.addons.library.models.loan import LibraryLoan
from odoo.addons.library.models.material import LibraryMaterial

INPUT_PATH = '/opt/odoo_mis_community_v19/migration_data/library/library_export.json'
# Bien moi truong LIBRARY_EXPORT_JSON (neu co) ghi de INPUT_PATH - khoi sua code
# khi chay test / production (odoo-bin shell khong nhan tham so cho script).
INPUT_PATH = os.environ.get('LIBRARY_EXPORT_JSON') or INPUT_PATH
IMPORT_MODULE = 'library_migration'   # ten "module" gia de luu external id chong trung
COMMIT_EVERY = 50                     # commit sau moi N tai lieu (giai phong bo nho file)
# Tai lieu PDF khong co anh bia cu: de module tu trich trang dau lam anh bia
# (can PyMuPDF, cham hon). False = bo qua, co the bam "Lay anh bia" sau.
EXTRACT_COVER_IF_MISSING = True
# Doc gia thieu email: None = bo qua (kem the + phieu muon cua ho).
# Hoac mau, vd '{code}@eaut.edu.vn', de tu sinh email.
MISSING_EMAIL_TEMPLATE = None
PAYMENT_METHOD_FOR_PAID = 'other'
MIGRATION_NOTE = 'Chuyen tu he thong thu vien cu (OpenEduCat)'

with open(INPUT_PATH, 'r', encoding='utf-8') as f:
    payload = json.load(f)

env = env(context=dict(env.context, tracking_disable=True, mail_create_nolog=True,
                       mail_create_nosubscribe=True, mail_notrack=True))

ModelData = env['ir.model.data']
Industry = env['library.industry']
Category = env['library.category']
Material = env['library.material']
Unit = env['library.material.unit']
CardType = env['library.card.type']
Member = env['library.member']
Card = env['library.card']
Loan = env['library.loan']
Fine = env['library.fine']
Sequence = env['ir.sequence']

errors = []          # (nhan, loi)
counters = {}


def count(key, n=1):
    counters[key] = counters.get(key, 0) + n


def xmlid_name(sub, source):
    name = '%s_%s' % (sub, source)
    return ''.join(c if (c.isalnum() or c == '_') else '_' for c in name)[:150]


def find_by_xmlid(model_name, sub, source):
    data = ModelData.search([('module', '=', IMPORT_MODULE), ('name', '=', xmlid_name(sub, source))], limit=1)
    if data:
        return env[model_name].browse(data.res_id).exists()
    return env[model_name]


def register_xmlid(model_name, sub, source, record):
    ModelData.create({
        'module': IMPORT_MODULE, 'name': xmlid_name(sub, source),
        'model': model_name, 'res_id': record.id, 'noupdate': True,
    })


def run_safe(label, fn):
    """Chay fn trong savepoint; loi thi rollback rieng ban ghi do va ghi lai."""
    try:
        with env.cr.savepoint():
            return fn()
    except Exception as e:  # noqa: BLE001
        errors.append((label, repr(e)))
        return None


def read_file_b64(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return base64.b64encode(fh.read())


_name_cache = {}


def get_named_ids(model_name, names):
    """id cac ban ghi theo ten (=ilike), tao moi neu chua co. Goi NGOAI savepoint
    cua tai lieu de cache khong giu id cua ban ghi bi rollback."""
    ids = []
    for name in names or []:
        key = (model_name, name.lower())
        if key not in _name_cache:
            rec = env[model_name].search([('name', '=ilike', name)], limit=1) \
                or env[model_name].create({'name': name})
            _name_cache[key] = rec.id
        if _name_cache[key] not in ids:
            ids.append(_name_cache[key])
    return ids


# ============================================================
# 1. Nganh + danh muc
# ============================================================
industry_by_source = {}
for entry in payload['industries']:
    rec = find_by_xmlid('library.industry', 'industry', entry['source_id'])
    if not rec:
        code = entry['code'] or 'N%s' % entry['source_id']
        rec = Industry.search([('code', '=', code)], limit=1) \
            or Industry.create({'name': entry['name'], 'code': code})
        register_xmlid('library.industry', 'industry', entry['source_id'], rec)
        count('industry')
    industry_by_source[entry['source_id']] = rec.id

category_by_source = {}
pending = list(payload['categories'])
known_sources = {c['source_id'] for c in pending}
while pending:
    progressed = False
    for entry in list(pending):
        parent_src = entry['parent_source_id'] if entry['parent_source_id'] in known_sources else None
        if parent_src and parent_src not in category_by_source:
            continue   # cho cha duoc tao truoc
        parent_id = category_by_source.get(parent_src) or False
        rec = find_by_xmlid('library.category', 'category', entry['source_id'])
        if not rec:
            rec = Category.search([('name', '=', entry['name']), ('parent_id', '=', parent_id)], limit=1) \
                or Category.create({'name': entry['name'], 'code': entry['code'] or False,
                                    'parent_id': parent_id})
            register_xmlid('library.category', 'category', entry['source_id'], rec)
            count('category')
        category_by_source[entry['source_id']] = rec.id
        pending.remove(entry)
        progressed = True
    if not progressed:   # vong lap cha-con: tao khong cha
        for entry in pending:
            entry['parent_source_id'] = None
env.cr.commit()
print("Nganh: %d, danh muc: %d (tao moi %d / %d)" % (
    len(industry_by_source), len(category_by_source), counters.get('industry', 0), counters.get('category', 0)))

# ============================================================
# 2. Tai lieu + don vi tai lieu
# ============================================================
lang_cache = {}


def lang_id(code):
    if not code:
        return False
    if code not in lang_cache:
        lang_cache[code] = env['res.lang'].with_context(active_test=False).search(
            [('code', '=', code)], limit=1).id or False
    return lang_cache[code]


unit_by_source = {}
deferred_units = []   # (material_id, unit) cua don vi tai lieu khong co barcode cu
material_ids_imported = []
processed = 0
for entry in payload['materials']:
    existing = find_by_xmlid('library.material', 'material', entry['source_key'])
    if existing:
        count('material_skip')
        for u in entry['units']:
            unit = find_by_xmlid('library.material.unit', 'unit', u['source_id'])
            if unit:
                unit_by_source[u['source_id']] = unit.id
            elif not u['barcode']:
                deferred_units.append((existing.id, u))   # lan truoc loi o luot sinh barcode
        continue

    # Gia tri many2many + cau hinh: resolve NGOAI savepoint
    m2m = {
        'author_ids': get_named_ids('library.author', entry['author_names']),
        'additional_author_ids': get_named_ids('library.author', entry['additional_author_names']),
        'corporate_author_ids': get_named_ids('library.corporate.author', entry['corporate_author_names']),
        'publisher_ids': get_named_ids('library.publisher', entry['publisher_names']),
        'tag_ids': get_named_ids('library.tag', entry['tag_names']),
    }

    def create_material(entry=entry, m2m=m2m):
        if entry.get('description_html'):
            description = entry['description_html']
        elif entry.get('description_text'):
            description = plaintext2html(entry['description_text'])
        else:
            description = False
        vals = {
            'name': entry['name'],
            'isbn': entry['isbn'] or False,
            'internal_code': entry['internal_code'] or False,
            'edition': entry['edition'] or False,
            'description': description,
            'active': entry['active'],
            'category_id': category_by_source.get(entry['category_source_id']) or False,
            'industry_id': industry_by_source.get(entry['industry_source_id']) or False,
            'language_id': lang_id(entry['language_code']),
            'num_pages': entry['num_pages'] or 0,
            'price': entry['price'] or 0.0,
            **{f: entry[f] or False for f in (
                'ddc', 'cutter_number', 'cataloging_agency_code', 'language_code', 'country_code',
                'subtitle', 'volume_name', 'volume_number', 'publication_place', 'publish_year', 'size')},
            **{f: [(6, 0, ids)] for f, ids in m2m.items()},
        }
        digital = entry.get('digital')
        if digital:
            datas = read_file_b64(digital['file'])
            if datas:
                vals.update({
                    'digital_type': digital['type'], 'digital_file': datas,
                    'digital_filename': digital['filename'],
                    'digital_source': digital['source'] or False,
                    'publish_online': digital['publish_online'],
                })
            else:
                count('digital_file_missing')
        cover = read_file_b64(entry.get('cover_file'))
        if cover:
            vals['cover_image'] = cover
        if cover or EXTRACT_COVER_IF_MISSING:
            material = Material.create(vals)
        else:
            # LibraryMaterial.create() chi lam 1 viec: trich anh bia tu PDF khi
            # thieu cover_image. Goi lop cha de bo qua buoc nay.
            material = super(LibraryMaterial, Material).create([vals])
        register_xmlid('library.material', 'material', entry['source_key'], material)

        unit_vals = []
        unit_sources = []
        blank_units = []
        for u in entry['units']:
            if not u['barcode']:
                # Don vi tai lieu KHONG co barcode cu: module tu sinh "<ky hieu>-<so>"
                # tu so lon nhat DANG CO. Tao o luot rieng SAU KHI nhap het barcode
                # cu, neu khong so sinh ra co the trung 1 barcode cu nhap sau.
                blank_units.append(u)
                continue
            same = Unit.with_context(active_test=False).search([('barcode', '=', u['barcode'])], limit=1)
            if same:
                raise ValueError('Barcode %s da ton tai o DB dich (unit id %s)' % (u['barcode'], same.id))
            unit_vals.append({
                'name': u['name'], 'material_id': material.id,
                'barcode': u['barcode'] or False, 'state': u['state'], 'active': u['active'],
                'note': MIGRATION_NOTE,
            })
            unit_sources.append(u['source_id'])
        units = Unit.create(unit_vals) if unit_vals else Unit
        for source_id, unit in zip(unit_sources, units):
            register_xmlid('library.material.unit', 'unit', source_id, unit)
        return material, dict(zip(unit_sources, units.ids)), blank_units

    result = run_safe('Tai lieu %s "%s"' % (entry['source_key'], entry['name']), create_material)
    if result:
        material, unit_map, blank_units = result
        unit_by_source.update(unit_map)
        deferred_units.extend((material.id, u) for u in blank_units)
        material_ids_imported.append(material.id)
        count('material')
        count('unit', len(unit_map))
    processed += 1
    if processed % COMMIT_EVERY == 0:
        env.cr.commit()
        env.invalidate_all()
        print("  ... %d/%d tai lieu" % (processed, len(payload['materials'])))
env.cr.commit()

# Luot rieng: don vi tai lieu khong co barcode cu (sau khi da co du barcode cu)
for material_id, u in deferred_units:
    def create_blank_unit(material_id=material_id, u=u):
        unit = Unit.create({
            'name': u['name'], 'material_id': material_id, 'barcode': False,
            'state': u['state'], 'active': u['active'], 'note': MIGRATION_NOTE,
        })
        register_xmlid('library.material.unit', 'unit', u['source_id'], unit)
        return unit
    unit = run_safe('Don vi tai lieu #%s "%s" (sinh barcode moi)' % (u['source_id'], u['name']),
                    create_blank_unit)
    if unit:
        unit_by_source[u['source_id']] = unit.id
        count('unit')
        count('unit_new_barcode')
        print("  Sinh barcode moi %s cho don vi tai lieu #%s" % (unit.barcode, u['source_id']))
env.cr.commit()
print("Tai lieu: tao moi %d, bo qua %d (da nhap); don vi tai lieu tao moi %d (sinh barcode moi %d)" % (
    counters.get('material', 0), counters.get('material_skip', 0), counters.get('unit', 0),
    counters.get('unit_new_barcode', 0)))

# ============================================================
# 3. Loai the, doc gia, the
# ============================================================
card_type_by_source = {}
for entry in payload['card_types']:
    rec = find_by_xmlid('library.card.type', 'card_type', entry['source_id'])
    if not rec:
        rec = CardType.search([('name', '=', entry['name'])], limit=1) or CardType.create({
            'name': entry['name'], 'max_loans': entry['max_loans'],
            'loan_days': entry['loan_days'], 'penalty_per_day': entry['penalty_per_day'],
        })
        register_xmlid('library.card.type', 'card_type', entry['source_id'], rec)
        count('card_type')
    card_type_by_source[entry['source_id']] = rec.id

member_by_key = {}
for entry in payload['members']:
    rec = find_by_xmlid('library.member', 'member', entry['source_key'])
    if rec:
        member_by_key[entry['source_key']] = rec.id
        continue
    email = entry['email']
    if not email and MISSING_EMAIL_TEMPLATE:
        email = MISSING_EMAIL_TEMPLATE.format(code=entry['code'])
    if not email:
        errors.append(('Doc gia %s (%s)' % (entry['name'], entry['code']), 'Thieu email - bo qua'))
        continue

    def create_member(entry=entry, email=email):
        code = entry['code']
        same_code = Member.with_context(active_test=False).search([('code', '=', code)], limit=1)
        if same_code:
            if (same_code.email or '').lower() == email:
                register_xmlid('library.member', 'member', entry['source_key'], same_code)
                return same_code   # da co san (vd nhap tay truoc do) -> dung lai
            code = '%s-%s' % (code, entry['source_key'].split('_')[-1])
        member = Member.create({
            'name': entry['name'], 'code': code, 'member_type': entry['member_type'],
            'state': 'active', 'email': email, 'phone': entry.get('phone') or False,
            'gender': entry.get('gender') or False, 'birthday': entry.get('birthday') or False,
            'id_number': entry.get('id_number') or False, 'address': entry.get('address') or False,
            'class_name': entry.get('class_name') or False, 'major': entry.get('major') or False,
            'department': entry.get('department') or False,
            'academic_year': entry.get('academic_year') or False,
            'note': MIGRATION_NOTE,
        })
        register_xmlid('library.member', 'member', entry['source_key'], member)
        return member

    member = run_safe('Doc gia %s (%s)' % (entry['name'], entry['code']), create_member)
    if member:
        member_by_key[entry['source_key']] = member.id
        count('member')
env.cr.commit()

card_by_source = {}
# Tao the "Da huy" truoc the "Dang dung" de rang buoc 1 the dang dung khong vuong
for entry in sorted(payload['cards'], key=lambda c: c['state'] == 'active'):
    rec = find_by_xmlid('library.card', 'card', entry['source_id'])
    if rec:
        card_by_source[entry['source_id']] = rec.id
        continue
    member_id = member_by_key.get(entry['member_key'])
    card_type_id = card_type_by_source.get(entry['card_type_source_id'])
    if not member_id or not card_type_id:
        errors.append(('The #%s so %s' % (entry['source_id'], entry['number']),
                       'Khong co doc gia/loai the tuong ung - bo qua'))
        continue

    def create_card(entry=entry, member_id=member_id, card_type_id=card_type_id):
        number = entry['number']
        if number and Card.with_context(active_test=False).search_count([('number', '=', number)]):
            number = Card._make_unique_number(number)
        vals = {
            'member_id': member_id, 'card_type_id': card_type_id,
            'state': entry['state'], 'active': entry['active'],
        }
        if number:
            vals['number'] = number
        if entry['issue_date']:
            vals['issue_date'] = entry['issue_date']
        card = Card.create(vals)
        register_xmlid('library.card', 'card', entry['source_id'], card)
        return card

    card = run_safe('The #%s so %s' % (entry['source_id'], entry['number']), create_card)
    if card:
        card_by_source[entry['source_id']] = card.id
        count('card')
env.cr.commit()
print("Loai the tao moi %d; doc gia tao moi %d; the tao moi %d" % (
    counters.get('card_type', 0), counters.get('member', 0), counters.get('card', 0)))

# ============================================================
# 4. Phieu muon + khoan phat
# ============================================================
def next_name(code, date):
    return Sequence.with_context(ir_sequence_date=date).next_by_code(code) or '/'


for entry in payload['loans']:
    if find_by_xmlid('library.loan', 'loan', entry['source_id']):
        count('loan_skip')
        continue
    member_id = member_by_key.get(entry['member_key'])
    unit_id = unit_by_source.get(entry['unit_source_id'])
    if not member_id or not unit_id:
        errors.append(('Phieu muon movement #%s' % entry['source_id'],
                       'Khong co doc gia/don vi tai lieu tuong ung - bo qua'))
        continue

    def create_loan(entry=entry, member_id=member_id, unit_id=unit_id):
        member = Member.browse(member_id)
        vals = {
            'name': next_name('library.loan', entry['issue_date']),
            'member_id': member_id,
            'card_id': card_by_source.get(entry['card_source_id']) or member.card_id.id or False,
            'unit_id': unit_id,
            'issue_date': entry['issue_date'],
            'due_date': entry['due_date'],
            'return_date': entry['return_date'] or False,
            'state': entry['state'],
            'late_days': entry['late_days'],
            'condition': 'good' if entry['state'] == 'returned' else False,
            'class_name': member.class_name or False,
            'cohort': member.cohort or False,
            'department': member.department or False,
            'active': entry['active'],
        }
        # Bo qua LibraryLoan.create (quy tac muon moi + doi trang thai don vi tai lieu)
        loan = super(LibraryLoan, Loan).create([vals])
        register_xmlid('library.loan', 'loan', entry['source_id'], loan)

        fine = entry.get('fine')
        if fine:
            paid = fine['state'] == 'paid'
            fine_date = fine['paid_date'] or entry['return_date'] or entry['issue_date']
            Fine.create({
                'name': next_name('library.fine', fine_date),
                'member_id': member_id, 'loan_id': loan.id, 'fine_type': 'late',
                'amount': fine['amount'], 'state': fine['state'],
                'payment_method': PAYMENT_METHOD_FOR_PAID if paid else False,
                'paid_date': fine['paid_date'] if paid else False,
                'note': '%s. Tra muon %s ngay.' % (MIGRATION_NOTE, entry['late_days']),
            })
            count('fine')
        return loan

    if run_safe('Phieu muon movement #%s' % entry['source_id'], create_loan):
        count('loan')
        if counters['loan'] % 500 == 0:
            env.cr.commit()
            print("  ... %d phieu muon" % counters['loan'])
env.cr.commit()
print("Phieu muon tao moi %d, bo qua %d; khoan phat tao moi %d" % (
    counters.get('loan', 0), counters.get('loan_skip', 0), counters.get('fine', 0)))

# ============================================================
# 5. Doi soat
# ============================================================
env.invalidate_all()


def imported_count(model_name):
    return ModelData.search_count([('module', '=', IMPORT_MODULE), ('model', '=', model_name)])


stats = payload['stats']
expected_members = len([m for m in payload['members'] if m['email'] or MISSING_EMAIL_TEMPLATE])
rows = [
    ('Tai lieu', stats['materials'], imported_count('library.material')),
    ('Don vi tai lieu', stats['units'], imported_count('library.material.unit')),
    ('Doc gia (co email)', expected_members, imported_count('library.member')),
    ('The', stats['cards'], imported_count('library.card')),
    ('Phieu muon', stats['loans'], imported_count('library.loan')),
]
print("\n===== DOI SOAT (file export -> DB dich) =====")
for label, expected, actual in rows:
    print("%-22s %8s -> %8s %s" % (label, expected, actual, 'OK' if expected == actual else '<-- LECH'))

loan_ids = ModelData.search([('module', '=', IMPORT_MODULE), ('model', '=', 'library.loan')]).mapped('res_id')
loans = Loan.with_context(active_test=False).browse(loan_ids)
open_actual = len(loans.filtered(lambda l: l.state == 'issue'))
print("%-22s %8s -> %8s %s" % ('Phieu dang muon', stats['open_loans'], open_actual,
                                'OK' if stats['open_loans'] == open_actual else '<-- LECH'))
unpaid_actual = sum(loans.fine_ids.filtered(lambda f: f.state == 'unpaid').mapped('amount'))
print("%-22s %8s -> %8s %s" % ('Tong no phat', stats['unpaid_fine_total'], unpaid_actual,
                                'OK' if abs(stats['unpaid_fine_total'] - unpaid_actual) < 0.01 else '<-- LECH'))

expected_barcodes = {u['barcode'] for m in payload['materials'] for u in m['units'] if u['barcode']}
unit_ids = ModelData.search([('module', '=', IMPORT_MODULE), ('model', '=', 'library.material.unit')]).mapped('res_id')
actual_barcodes = set(Unit.with_context(active_test=False).browse(unit_ids).mapped('barcode'))
missing_barcodes = expected_barcodes - actual_barcodes
print("%-22s %8s -> %8s %s" % ('Barcode giu nguyen', len(expected_barcodes),
                                len(expected_barcodes & actual_barcodes),
                                'OK' if not missing_barcodes else '<-- THIEU %d' % len(missing_barcodes)))
issue_units = Unit.with_context(active_test=False).browse(unit_ids).filtered(lambda u: u.state == 'issue')
if len(issue_units) != open_actual:
    print("CANH BAO: %d don vi tai lieu 'Dang muon' nhung co %d phieu dang muon" % (len(issue_units), open_actual))

if errors:
    print("\n===== %d LOI (ban ghi bi bo qua, cac ban ghi khac van nhap binh thuong) =====" % len(errors))
    for label, err in errors:
        print("  - %s: %s" % (label, err))
    print("Sua nguyen nhan roi chay lai script - ban ghi da nhap se tu bo qua.")
print("\nXong.")
