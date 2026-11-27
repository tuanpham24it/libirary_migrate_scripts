# Script NHAP giao dien Website thu vien (da xuat boi export_from_restored_db.py)
# vao DB Odoo 19 co eaut_library. Dua tren
# odoo_19_community/migration_scripts/website/import_to_website.py (ban MIS).
#
# CAN CAI TRUOC tren DB dich: module 'website' (+ 'library' neu muon link toi
# cong doc gia /my/library). Vd:
#   odoo-bin -c <odoo19.conf> -d <db> -i website,library --load-language=vi_VN --stop-after-init --no-http
# CHAY:
#   set WEBSITE_EXPORT_JSON=<thu_muc>\website_export.json
#   odoo-bin shell -c <odoo19.conf> -d <db> --no-http < migrate_scripts\website\import_to_website.py
#
# AN TOAN KHI CHAY LAI: view/page/menu danh dau bang external id (module gia
# IMPORT_MODULE); file SCSS tuy chinh duoc GHI DE (khong tao trung).
#
# KHAC BAN MIS:
#   - View tao THEO THU TU cha truoc con, gan inherit_id NGAY KHI TAO (view ke
#     thua dang <xpath> khong hop le neu tao ma chua co view cha).
#   - Noi dung view ghi DUNG TUNG BAN DICH (en_US, vi_VN...) nhu nguon.
#   - View/menu/snippet thuoc module KHONG CAI o dich (blog, eLearning...) bi
#     bo qua thay vi tao ra view hong / menu 404.
#   - Link cu cua thu vien so (/digital-library, /my-library) doi sang cong doc
#     gia moi cua eaut_library (URL_MAP).
#   - Menu goc cua website nguon gop vao menu goc SAN CO cua website dich (Odoo
#     chi hien 1 menu goc/website); menu mac dinh cua website dich (Home,
#     Contact us...) bi xoa lan dau de khong trung (REPLACE_DEFAULT_MENUS).
#   - Logo, favicon, mang xa hoi, ngon ngu, ten mien, bang mau SCSS tuy chinh.

import base64
import json
import os
import re

from lxml import etree

INPUT_PATH = '/opt/odoo_mis_community_v19/migration_data/website_library/website_export.json'
INPUT_PATH = os.environ.get('WEBSITE_EXPORT_JSON') or INPUT_PATH
IMPORT_MODULE = 'website_library_migration'

# Ghi ten mien nguon (vd https://thuvieneaut.edu.vn) vao website dich. Dat
# False khi chay tren DB TEST de khong anh huong cach Odoo chon website theo host.
COPY_DOMAIN = True
# Lan dau nhap: xoa menu con mac dinh cua website dich (Home, Contact us...) de
# thay bang menu cua website nguon.
REPLACE_DEFAULT_MENUS = True
# Link cu -> link moi (so khop TIEN TO duong dan; phan con lai bi bo vi id tai
# lieu cu khong con y nghia).
URL_MAP = [
    ('/digital-library', '/my/library/catalog'),
    ('/my-library', '/my/library'),
]
# Snippet / menu / view can module nao - module khong cai o dich thi bo qua.
SNIPPET_MODULES = {
    's_blog_posts': 'website_blog', 's_dynamic_snippet_products': 'website_sale',
    's_products_searchbar': 'website_sale', 's_events': 'website_event',
    's_dynamic_snippet_courses': 'website_slides',
}
MENU_URL_MODULES = {
    '/blog': 'website_blog', '/slides': 'website_slides', '/shop': 'website_sale',
    '/event': 'website_event', '/forum': 'website_forum', '/jobs': 'website_hr_recruitment',
}
# Link mang xa hoi mac dinh cua Odoo (chua ai sua) - khong chep sang.
SOCIAL_PLACEHOLDER_RE = re.compile(r'odoo|openerp', re.I)

with open(INPUT_PATH, 'r', encoding='utf-8') as f:
    payload = json.load(f)

Website = env['website']
View = env['ir.ui.view']
Page = env['website.page']
Menu = env['website.menu']
Attachment = env['ir.attachment']
Lang = env['res.lang']
ModelData = env['ir.model.data']
Module = env['ir.module.module']

if 'website' not in env.registry:
    raise SystemExit("DB dich chua cai module 'website'. Cai truoc: odoo-bin ... -i website")

_installed = {}


def installed(module_name):
    if module_name not in _installed:
        _installed[module_name] = bool(Module.search_count(
            [('name', '=', module_name), ('state', '=', 'installed')]))
    return _installed[module_name]


def find_by_xmlid(model_name, xmlid_name):
    data = ModelData.search([('module', '=', IMPORT_MODULE), ('name', '=', xmlid_name)], limit=1)
    if data:
        return env[model_name].browse(data.res_id).exists()
    return env[model_name].browse()


def register_xmlid(model_name, xmlid_name, record):
    ModelData.create({'module': IMPORT_MODULE, 'name': xmlid_name, 'model': model_name,
                      'res_id': record.id, 'noupdate': True})


def read_b64(entry):
    path = (entry or {}).get('file')
    if not path or not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return base64.b64encode(fh.read())


def map_url(url):
    if not url:
        return url
    for old, new in URL_MAP:
        if url == old or url.startswith(old + '/') or url.startswith(old + '?'):
            return new
    return url


def url_module_missing(url):
    for prefix, module_name in MENU_URL_MODULES.items():
        if url and (url == prefix or url.startswith(prefix + '/') or url.startswith(prefix + '?')):
            if not installed(module_name):
                return module_name
    return None


report = []
active_langs = set(Lang.search([]).mapped('code'))

# ============================================================
# 1. Website: doi chieu website dich, cap nhat thong tin
# ============================================================
existing_websites = Website.search([], order='id')
website_id_map = {}
for index, entry in enumerate(payload['websites']):
    website = existing_websites[index] if index < len(existing_websites) \
        else Website.create({'name': entry['name']})
    vals = {'name': entry['name']}
    if COPY_DOMAIN and entry.get('domain'):
        vals['domain'] = entry['domain']
    # Ngon ngu: kich hoat neu chua co
    lang_codes = [c for c in (entry.get('lang_codes') or []) + [entry.get('default_lang_code')] if c]
    lang_ids = []
    for code in dict.fromkeys(lang_codes):
        lang = Lang.with_context(active_test=False).search([('code', '=', code)], limit=1)
        if lang and not lang.active:
            env['base.language.install'].create({'lang_ids': [(6, 0, lang.ids)]}).lang_install()
            report.append("Da kich hoat ngon ngu %s" % code)
        if lang:
            lang_ids.append(lang.id)
    if lang_ids:
        vals['language_ids'] = [(6, 0, lang_ids)]
    if entry.get('default_lang_code'):
        default_lang = Lang.search([('code', '=', entry['default_lang_code'])], limit=1)
        if default_lang:
            vals['default_lang_id'] = default_lang.id
    for field in ('social_facebook', 'social_twitter', 'social_linkedin', 'social_youtube',
                  'social_instagram', 'social_github', 'social_tiktok', 'google_analytics_key'):
        value = entry.get(field)
        if field in website._fields and value:
            if field.startswith('social_') and SOCIAL_PLACEHOLDER_RE.search(value):
                report.append("Bo qua link mac dinh cua Odoo %s = %s" % (field, value))
                continue
            vals[field] = value
    for field in ('logo', 'favicon'):
        datas = read_b64(entry.get(field))
        if datas:
            vals[field] = datas
    website.write(vals)
    website_id_map[entry['source_id']] = website.id
    print("Website nguon #%s -> website dich #%s '%s'" % (entry['source_id'], website.id, entry['name']))
active_langs = set(Lang.search([]).mapped('code'))
default_website_id = existing_websites[:1].id or (list(website_id_map.values()) or [False])[0]


def target_website_id(source_website_id):
    return website_id_map.get(source_website_id) or default_website_id


# ============================================================
# 2. SCSS tuy chinh (bang mau, font...) - ghi de neu da co
# ============================================================
for asset in payload.get('custom_assets') or []:
    wid = target_website_id(asset['website_source_id'])
    try:
        with env.cr.savepoint():
            env['website.assets'].with_context(website_id=wid).save_asset(
                asset['url'], asset['bundle'], asset['content'], 'scss')
        print("SCSS tuy chinh: %s (website #%s)" % (asset['url'], wid))
    except Exception as exc:  # noqa: BLE001
        report.append("LOI SCSS tuy chinh %s: %s" % (asset['url'], exc))

# ============================================================
# 3. View: chuyen doi noi dung + tao theo thu tu cha truoc con
# ============================================================
attachment_id_map = {}


def get_or_create_attachment(source_att_id_str, image_entry):
    if source_att_id_str in attachment_id_map:
        return attachment_id_map[source_att_id_str]
    xmlid = 'attachment_%s' % source_att_id_str
    existing = find_by_xmlid('ir.attachment', xmlid)
    if existing:
        attachment_id_map[source_att_id_str] = existing.id
        return existing.id
    datas = read_b64(image_entry)
    if not datas:
        return None
    attachment = Attachment.create({
        'name': image_entry.get('name') or 'image_%s' % source_att_id_str,
        'datas': datas, 'mimetype': image_entry.get('mimetype') or False,
        'public': True, 'res_model': 'ir.ui.view',
    })
    register_xmlid('ir.attachment', xmlid, attachment)
    attachment_id_map[source_att_id_str] = attachment.id
    return attachment.id


def transform_arch(arch, image_files, label):
    # 1) Anh nhung: id cu -> id moi
    for old_id_str, image_entry in (image_files or {}).items():
        new_id = get_or_create_attachment(old_id_str, image_entry)
        if new_id:
            arch = re.sub(r'(?<!\d)/web/(image|content)/%s(?!\d)' % old_id_str, r'/web/\g<1>/%d' % new_id, arch)
    # 2) Link cu cua thu vien so -> cong doc gia moi
    arch = re.sub(r'(href=")([^"]*)(")', lambda m: m.group(1) + map_url(m.group(2)) + m.group(3), arch)
    # 3) Bo snippet can module khong cai
    removable = {s for s, mod in SNIPPET_MODULES.items() if ('data-snippet="%s"' % s) in arch and not installed(mod)}
    if removable:
        root = etree.fromstring(arch.encode('utf-8'))
        for node in root.xpath('//*[@data-snippet]'):
            if node.get('data-snippet') in removable and node.getparent() is not None:
                parent, tail = node.getparent(), node.tail or ''
                prev = node.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or '') + tail
                else:
                    parent.text = (parent.text or '') + tail
                parent.remove(node)
        arch = etree.tostring(root, encoding='unicode')
        note = "%s: da bo snippet %s (module chua cai)" % (label, ', '.join(sorted(removable)))
        if note not in report:
            report.append(note)
    return arch


views_by_source = {v['source_id']: v for v in payload['views']}
view_id_map = {}
created_views = skipped_views = failed_views = 0
pending = []
for entry in payload['views']:
    existing = find_by_xmlid('ir.ui.view', 'view_%s' % entry['source_id'])
    if existing:
        view_id_map[entry['source_id']] = existing.id
        skipped_views += 1
        continue
    module_name = (entry.get('key') or '').split('.')[0]
    if module_name and not installed(module_name):
        report.append("Bo qua view %s (module %s chua cai)" % (entry['key'], module_name))
        continue
    pending.append(entry)

while pending:
    progressed = False
    for entry in list(pending):
        parent_src = entry.get('inherit_source_id')
        parent_id = False
        if parent_src:
            if parent_src in views_by_source:
                if parent_src not in view_id_map:
                    if any(p['source_id'] == parent_src for p in pending):
                        continue   # cho view cha (cung website) duoc tao truoc
                    pending.remove(entry)
                    report.append("Bo qua view '%s': view cha bi bo qua" % entry['name'])
                    progressed = True
                    continue
                parent_id = view_id_map[parent_src]
            elif entry.get('inherit_key'):
                parent_id = View.search([('key', '=', entry['inherit_key']), ('website_id', '=', False)],
                                        limit=1).id
            if not parent_id:
                pending.remove(entry)
                report.append("Bo qua view '%s': khong tim thay view cha key=%s" % (
                    entry['name'], entry.get('inherit_key')))
                progressed = True
                continue
        pending.remove(entry)
        progressed = True

        label = '%s (#%s)' % (entry.get('key') or entry['name'], entry['source_id'])
        translations = {lang: xml for lang, xml in entry['arch_translations'].items()
                        if lang in active_langs or lang == 'en_US'}
        base_arch = translations.get('vi_VN') or translations.get('en_US') or next(iter(translations.values()))
        vals = {
            'name': entry['name'], 'type': entry.get('type') or 'qweb',
            'active': entry.get('active', True), 'priority': entry.get('priority') or 16,
            'mode': entry.get('mode') or 'primary',
            'website_id': target_website_id(entry.get('website_source_id')),
        }
        if entry.get('key'):
            vals['key'] = entry['key']
        if parent_id:
            vals['inherit_id'] = parent_id
        try:
            with env.cr.savepoint():
                base_arch = transform_arch(base_arch, entry.get('image_files'), label)
                # Website dich co the DA CO ban rieng cung key (vd Odoo 19 tu tao
                # website.homepage rieng khi cai website) -> ghi de len ban do,
                # tao them se ra 2 ban va Odoo hien ban cu.
                same = View.search([('key', '=', entry['key']), ('website_id', '=', vals['website_id'])],
                                   limit=1) if entry.get('key') else View
                if same:
                    same.with_context(lang='en_US').write(dict(vals, arch=base_arch))
                    view = same
                    report.append("Ghi de view co san cua website dich: %s (#%s)" % (entry['key'], same.id))
                else:
                    # create() lay key 'arch' ra khoi dict vals - khong doc lai vals['arch']
                    view = View.with_context(lang='en_US').create(dict(vals, arch=base_arch))
                # Ghi dung tung ban dich nhu nguon (arch_db la jsonb {lang: xml})
                if len(translations) > 1:
                    arch_db = {lang: transform_arch(xml, entry.get('image_files'), label)
                               for lang, xml in translations.items()}
                    arch_db.setdefault('en_US', base_arch)
                    env.cr.execute("UPDATE ir_ui_view SET arch_db = %s::jsonb WHERE id = %s",
                                   (json.dumps(arch_db, ensure_ascii=False), view.id))
                    view.invalidate_recordset(['arch_db', 'arch'])
                register_xmlid('ir.ui.view', 'view_%s' % entry['source_id'], view)
        except Exception as exc:  # noqa: BLE001
            report.append("LOI tao view %s: %s" % (label, exc))
            failed_views += 1
            continue
        view_id_map[entry['source_id']] = view.id
        created_views += 1
    if not progressed:
        for entry in pending:
            report.append("Bo qua view '%s': vong lap ke thua" % entry['name'])
        break

# ============================================================
# 4. Trang
# ============================================================
page_id_map = {}
created_pages = skipped_pages = 0
for entry in payload['pages']:
    existing = find_by_xmlid('website.page', 'page_%s' % entry['source_id'])
    if existing:
        page_id_map[entry['source_id']] = existing.id
        skipped_pages += 1
        continue
    view_target_id = view_id_map.get(entry['view_source_id'])
    if not view_target_id:
        report.append("Bo qua trang %s: view khong duoc tao" % entry['url'])
        continue
    vals = {'view_id': view_target_id, 'url': entry['url'] or False,
            'is_published': entry['is_published'],
            'website_id': target_website_id(entry['website_source_id'])}
    if 'website_indexed' in Page._fields:
        vals['website_indexed'] = entry['website_indexed']
    try:
        with env.cr.savepoint():
            same = Page.search([('url', '=', vals['url']), ('website_id', '=', vals['website_id'])], limit=1)
            if same:   # trang rieng san co cua website dich (cung URL) -> dung lai
                same.write(vals)
                page = same
            else:
                page = Page.create(vals)
            register_xmlid('website.page', 'page_%s' % entry['source_id'], page)
    except Exception as exc:  # noqa: BLE001
        report.append("LOI tao trang %s: %s" % (entry['url'], exc))
        continue
    page_id_map[entry['source_id']] = page.id
    created_pages += 1

# Don ban trung con sot (vd lan chay truoc tao them thay vi ghi de): trang/view
# rieng cung URL/key tren cung website nhung KHONG phai ban da nhap -> xoa.
our_pages = Page.browse(list(page_id_map.values())).exists()
our_views = View.browse(list(view_id_map.values())).exists()
for page in our_pages:
    dups = Page.search([('url', '=', page.url), ('website_id', '=', page.website_id.id), ('id', '!=', page.id)])
    if dups:
        report.append("Xoa trang trung %s (#%s)" % (page.url, ', '.join(map(str, dups.ids))))
        dups.unlink()
for view in our_views.filtered('key'):
    dups = View.search([('key', '=', view.key), ('website_id', '=', view.website_id.id),
                        ('id', 'not in', our_views.ids)])
    if dups:
        report.append("Xoa view trung %s (#%s)" % (view.key, ', '.join(map(str, dups.ids))))
        dups.unlink()

# ============================================================
# 5. Menu: menu goc nguon -> menu goc san co cua website dich
# ============================================================
menu_id_map = {}
created_menus = skipped_menus = 0
for entry in payload['menus']:
    if entry['parent_source_id']:
        continue
    website = Website.browse(target_website_id(entry['website_source_id']))
    xmlid = 'menu_%s' % entry['source_id']
    top = find_by_xmlid('website.menu', xmlid)
    if not top:
        top = website.menu_id or Menu.create({'name': entry['name'], 'website_id': website.id})
        if REPLACE_DEFAULT_MENUS:
            ours = set(ModelData.search([('module', '=', IMPORT_MODULE), ('model', '=', 'website.menu')])
                       .mapped('res_id'))
            defaults = Menu.search([('parent_id', '=', top.id), ('id', 'not in', list(ours))])
            if defaults:
                report.append("Xoa menu mac dinh cua website dich: %s" % ', '.join(defaults.mapped('name')))
                defaults.unlink()
        register_xmlid('website.menu', xmlid, top)
    menu_id_map[entry['source_id']] = top.id

children = [m for m in payload['menus'] if m['parent_source_id']]
progress = True
while children and progress:
    progress = False
    for entry in list(children):
        parent_id = menu_id_map.get(entry['parent_source_id'])
        if not parent_id:
            if not any(c['source_id'] == entry['parent_source_id'] for c in children):
                children.remove(entry)
                report.append("Bo qua menu '%s': khong co menu cha" % entry['name'])
                progress = True
            continue
        children.remove(entry)
        progress = True
        existing = find_by_xmlid('website.menu', 'menu_%s' % entry['source_id'])
        if existing:
            menu_id_map[entry['source_id']] = existing.id
            skipped_menus += 1
            # Lan truoc trang chua tao duoc -> gan lai trang cho menu
            page_id = page_id_map.get(entry['page_source_id'])
            if page_id and existing.page_id.id != page_id:
                existing.page_id = page_id
            continue
        missing_module = url_module_missing(entry['url'])
        if missing_module:
            report.append("Bo qua menu '%s' (%s): module %s chua cai" % (entry['name'], entry['url'], missing_module))
            continue
        vals = {
            'name': entry['name'], 'url': map_url(entry['url']) or False,
            'sequence': entry['sequence'], 'new_window': entry['new_window'],
            'parent_id': parent_id, 'website_id': target_website_id(entry['website_source_id']),
        }
        if entry['page_source_id'] and page_id_map.get(entry['page_source_id']):
            vals['page_id'] = page_id_map[entry['page_source_id']]
        try:
            with env.cr.savepoint():
                menu = Menu.with_context(lang='en_US').create(vals)
                for lang, name in (entry.get('name_translations') or {}).items():
                    if lang in active_langs and lang != 'en_US' and name:
                        menu.with_context(lang=lang).name = name
                register_xmlid('website.menu', 'menu_%s' % entry['source_id'], menu)
        except Exception as exc:  # noqa: BLE001
            report.append("LOI tao menu '%s': %s" % (entry['name'], exc))
            continue
        menu_id_map[entry['source_id']] = menu.id
        created_menus += 1

# Bundle CSS/JS cu khong con dung (logo/mau/SCSS vua doi) - Odoo tu build lai
Attachment.search([('url', '=like', '/web/assets/%')]).unlink()

env.cr.commit()
print("\nView: tao moi %d, bo qua %d (da nhap), loi %d" % (created_views, skipped_views, failed_views))
print("Trang: tao moi %d, bo qua %d" % (created_pages, skipped_pages))
print("Menu: tao moi %d, bo qua %d" % (created_menus, skipped_menus))
print("Anh nhung: %d" % len(attachment_id_map))
if report:
    print("\n===== GHI CHU (%d) =====" % len(report))
    for line in report:
        print("  - %s" % line)
print("\nXong.")
