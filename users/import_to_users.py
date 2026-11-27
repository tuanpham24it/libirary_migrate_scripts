# Script NHAP tai khoan dang nhap (internal + portal user) da xuat boi
# export_from_restored_db.py vao DB Odoo 19, GIU NGUYEN HASH MAT KHAU => nguoi
# dung dang nhap bang mat khau cu, khong can doi/dat lai.
#
# CHAY SAU script library (de tai khoan sinh vien gan dung contact cua doc gia).
# Vd (Linux):
#   USERS_EXPORT_JSON=/opt/.../users_export.json \
#     odoo-bin shell -c <odoo19.conf> -d <db> --no-http < migrate_scripts/users/import_to_users.py
#
# CACH GIU MAT KHAU: ORM write({'password': ...}) coi gia tri la mat khau RO va
# hash lai => sai. Script ghi THANG chuoi hash vao cot res_users.password bang
# SQL (giong ban MIS). Odoo 18 va 19 cung dung pbkdf2_sha512 nen tuong thich.
#
# TAI KHOAN DA CO SAN o DB dich (trung login): KHONG tao trung, chi bo sung
# nhom quyen con thieu; mat khau CHI ghi khi tai khoan do chua co mat khau
# (tru khi bat OVERWRITE_EXISTING_PASSWORD) - tranh de mat khau nguoi dung da
# tu doi tren he thong moi.
#
# GAN VOI DOC GIA: tai khoan sinh vien/giang vien dung CHINH contact cua doc
# gia (library.member) da tao boi script library (external id
# library_migration.member_<source_key>) => library.member.user_id tu cap nhat,
# cong /my/library nhan ra tai khoan. Khong tim thay doc gia thi doi chieu
# contact theo email, cuoi cung moi tao contact moi.
#
# AN TOAN KHI CHAY LAI: moi tai khoan danh dau bang external id theo id nguon.

import base64
import json
import os

INPUT_PATH = '/opt/odoo_mis_community_v19/migration_data/users_library/users_export.json'
INPUT_PATH = os.environ.get('USERS_EXPORT_JSON') or INPUT_PATH
IMPORT_MODULE = 'users_library_migration'
LIBRARY_IMPORT_MODULE = 'library_migration'   # module gia cua script library
BATCH_SIZE = 200                              # so portal user tao moi lan (create multi)
OVERWRITE_EXISTING_PASSWORD = False

# Nhom quyen cu (OpenEduCat) -> nhom tuong ung cua eaut_library
GROUP_MAP = {
    'openeducat_library.group_op_library': 'library_student.group_library_manager',
    'openeducat_library.group_op_library_user': 'library_student.group_library_user',
    'openeducat_digital_library.group_digital_library_manager': 'library_student.group_library_manager',
    'openeducat_digital_library.group_digital_library_user': 'library_student.group_library_user',
}
# Nhom khong chep sang (chi co y nghia o he thong cu)
SKIP_GROUPS = {
    'website.group_multi_website',          # dich chi co 1 website
    'website.website_page_controller_expose',
}

with open(INPUT_PATH, 'r', encoding='utf-8') as f:
    payload = json.load(f)

env = env(context=dict(env.context, no_reset_password=True, tracking_disable=True,
                       mail_create_nolog=True, mail_create_nosubscribe=True, mail_notrack=True))
Users = env['res.users'].with_context(active_test=False)
Partner = env['res.partner']
ModelData = env['ir.model.data']
Lang = env['res.lang']

active_langs = set(Lang.search([]).mapped('code'))
report = []
unresolved_groups = {}
_group_cache = {}


def xmlid_record(module, name, model):
    data = ModelData.search([('module', '=', module), ('name', '=', name)], limit=1)
    return env[model].browse(data.res_id).exists() if data else env[model]


def register_xmlid(user, source_id):
    ModelData.create({'module': IMPORT_MODULE, 'name': 'user_%s' % source_id, 'model': 'res.users',
                      'res_id': user.id, 'noupdate': True})


def resolve_groups(entries, base_group):
    groups = env['res.groups'] | base_group
    for entry in entries or []:
        xmlid = entry.get('xmlid')
        if not xmlid or xmlid in SKIP_GROUPS or xmlid in ('base.group_user', 'base.group_portal',
                                                           'base.group_public'):
            continue
        xmlid = GROUP_MAP.get(xmlid, xmlid)
        if xmlid not in _group_cache:
            _group_cache[xmlid] = env.ref(xmlid, raise_if_not_found=False)
        group = _group_cache[xmlid]
        if group and group._name == 'res.groups':
            groups |= group
        else:
            unresolved_groups[xmlid] = unresolved_groups.get(xmlid, 0) + 1
    return groups


def member_partner(member_key):
    if not member_key:
        return Partner
    member = xmlid_record(LIBRARY_IMPORT_MODULE, 'member_%s' % member_key, 'library.member')
    return member.partner_id if member else Partner


def free_partner_by_email(email):
    """Contact co cung email va CHUA gan tai khoan nao (dung lai thay vi tao trung)."""
    if not email:
        return Partner
    partners = Partner.with_context(active_test=False).search([('email', '=ilike', email)])
    free = partners.filtered(lambda p: not p.user_ids)
    return free[:1]


def user_vals(entry, base_group, partner):
    vals = {
        'name': entry['name'], 'login': entry['login'],
        'active': entry.get('active', True),
        'group_ids': [(6, 0, resolve_groups(entry.get('groups'), base_group).ids)],
    }
    if partner:
        vals['partner_id'] = partner.id
    else:
        vals['email'] = entry.get('email') or False
    if entry.get('lang') in active_langs:
        vals['lang'] = entry['lang']
    if entry.get('tz'):
        vals['tz'] = entry['tz']
    if entry.get('signature'):
        vals['signature'] = entry['signature']
    return vals


def import_users(entries, base_group_xmlid, label):
    base_group = env.ref(base_group_xmlid)
    created = reused = failed = 0
    password_patches = []     # (user_id, hash)
    new_entries = []          # (entry, vals) cho tao moi theo lo
    taken_partner_ids = set()

    for entry in entries:
        user = xmlid_record(IMPORT_MODULE, 'user_%s' % entry['source_id'], 'res.users')
        if not user:
            user = Users.search([('login', '=', entry['login'])], limit=1)
            if user:
                register_xmlid(user, entry['source_id'])
        if user:
            reused += 1
            extra = resolve_groups(entry.get('groups'), base_group) - user.group_ids
            # Khong tron internal <-> portal: chi them nhom cung loai
            if user.share != (base_group_xmlid == 'base.group_portal'):
                extra = env['res.groups']
                report.append("%s: tai khoan %s da co o dich nhung khac loai (internal/portal) - giu nguyen"
                              % (label, entry['login']))
            if extra:
                user.write({'group_ids': [(4, gid) for gid in extra.ids]})
            if entry.get('password_hash'):
                env.cr.execute("SELECT password FROM res_users WHERE id = %s", (user.id,))
                if OVERWRITE_EXISTING_PASSWORD or not env.cr.fetchone()[0]:
                    password_patches.append((user.id, entry['password_hash']))
            continue

        partner = member_partner(entry.get('member_key'))
        if partner and (partner.user_ids or partner.id in taken_partner_ids):
            partner = Partner   # contact doc gia da gan tai khoan khac
        if not partner:
            partner = free_partner_by_email(entry.get('email'))
            if partner.id in taken_partner_ids:
                partner = Partner
        if partner:
            taken_partner_ids.add(partner.id)
        new_entries.append((entry, user_vals(entry, base_group, partner)))

    def create_one(entry, vals):
        try:
            with env.cr.savepoint():
                user = Users.create(vals)
                register_xmlid(user, entry['source_id'])
            return user
        except Exception as exc:  # noqa: BLE001
            report.append("%s: LOI tao %s: %s" % (label, entry['login'], exc))
            return None

    for start in range(0, len(new_entries), BATCH_SIZE):
        batch = new_entries[start:start + BATCH_SIZE]
        try:
            with env.cr.savepoint():
                users = Users.create([vals for _entry, vals in batch])
                for (entry, _vals), user in zip(batch, users):
                    register_xmlid(user, entry['source_id'])
            results = list(zip(batch, users))
        except Exception:  # noqa: BLE001 - lo loi: tao tung tai khoan de biet dong nao loi
            results = [((entry, vals), create_one(entry, vals)) for entry, vals in batch]
        for (entry, _vals), user in results:
            if not user:
                failed += 1
                continue
            created += 1
            if entry.get('password_hash'):
                password_patches.append((user.id, entry['password_hash']))
            if entry.get('image_file') and os.path.exists(entry['image_file']) and not user.image_1920:
                with open(entry['image_file'], 'rb') as fh:
                    user.partner_id.image_1920 = base64.b64encode(fh.read())
        env.cr.commit()
        print("  %s: %d/%d" % (label, min(start + BATCH_SIZE, len(new_entries)), len(new_entries)))

    # Ghi THANG hash mat khau (khong qua ORM - ORM se hash lai chuoi hash)
    for user_id, password_hash in password_patches:
        env.cr.execute("UPDATE res_users SET password = %s WHERE id = %s", (password_hash, user_id))
    env.cr.commit()
    env.invalidate_all()
    print("%s: tao moi %d, da co san %d, loi %d, ghi hash mat khau %d" % (
        label, created, reused, failed, len(password_patches)))
    return password_patches


internal_patches = import_users(payload.get('users') or [], 'base.group_user', 'Internal user')
portal_patches = import_users(payload.get('portal_users') or [], 'base.group_portal', 'Portal user')

# ============================================================
# Doi soat
# ============================================================
print("\n===== DOI SOAT =====")
all_entries = (payload.get('users') or []) + (payload.get('portal_users') or [])
expected_hash = {e['source_id']: e['password_hash'] for e in all_entries if e.get('password_hash')}
mapped = {d.name: d.res_id for d in ModelData.search([('module', '=', IMPORT_MODULE), ('model', '=', 'res.users')])}
hash_ok = hash_bad = 0
for source_id, password_hash in expected_hash.items():
    user_id = mapped.get('user_%s' % source_id)
    if not user_id:
        continue
    env.cr.execute("SELECT password FROM res_users WHERE id = %s", (user_id,))
    if env.cr.fetchone()[0] == password_hash:
        hash_ok += 1
    else:
        hash_bad += 1
print("Tai khoan da nhap:            %d / %d" % (sum(1 for e in all_entries if mapped.get('user_%s' % e['source_id'])),
                                              len(all_entries)))
print("Hash mat khau khop nguon:     %d / %d%s" % (hash_ok, len(expected_hash),
                                                 '' if not hash_bad else ' (%d giu mat khau dang co o dich)' % hash_bad))
if 'library.member' in env:
    members = env['library.member'].search([])
    print("Doc gia co tai khoan portal:  %d / %d" % (len(members.filtered('user_id')), len(members)))
no_password = sum(1 for e in payload.get('portal_users') or [] if not e.get('password_hash'))
if no_password:
    print("Portal user chua co mat khau: %d (nguon chua tung dat - dung 'Quen mat khau' de dat lan dau)"
          % no_password)
if unresolved_groups:
    report.append("Nhom quyen khong co o DB dich (module chua cai) - bo qua: %s" % ', '.join(
        '%s (%d)' % kv for kv in sorted(unresolved_groups.items())))
if report:
    print("\n===== GHI CHU (%d) =====" % len(report))
    for line in report[:200]:
        print("  - %s" % line)
print("\nXong. Nho XOA file JSON export (co hash mat khau).")
