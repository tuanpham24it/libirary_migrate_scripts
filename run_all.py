# Script TONG: chay toan bo pipeline migrate THU VIEN tu OpenEduCat (Odoo 18)
# sang eaut_library (Odoo 19) chi bang 1 lenh, theo dung thu tu:
#   cai module -> library (sach/doc gia/the/phieu muon) -> users (tai khoan + hash
#   mat khau) -> website.
# Cung cach dung voi odoo_19_community/migration_scripts/run_all.py (MIS).
#
# CACH CHAY (dung DUNG python trong venv cua Odoo 19 - can psycopg2, va
# sys.executable duoc dung de goi odoo-bin):
#   Windows:  E:\Odoo\odoo_19_community\venv\Scripts\python.exe libirary_migrate_scripts\run_all.py
#   Linux:    /opt/<odoo19>/venv/bin/python libirary_migrate_scripts/run_all.py
#
# TUY CHON:
#   --list                 chi liet ke cac buoc, khong chay gi
#   --only <module>        chi chay cac buoc cua 1 nhom: setup | library | users | website
#   --from <so_thu_tu>     chay tiep tu buoc N (vd loi o buoc 5, sua xong chay
#                          "--from 5" - CAC SCRIPT IMPORT DEU AN TOAN KHI CHAY LAI
#                          vi dung external id chong trung)
#   --db <ten_db>          ghi de DB dich (Odoo 19)
#   --conf <duong_dan>     ghi de file conf Odoo 19
#   --source-db <ten_db>   ghi de DB nguon (OpenEduCat da restore)
#   --source-conf <path>   ghi de file conf Odoo 18 (lay thong tin ket noi DB nguon)
#   --filestore <path>     ghi de thu muc filestore cua DB nguon
#   --data-dir <path>      ghi de thu muc chua file JSON trung gian
#   --yes                  khong hoi xac nhan (tu dong hoa)
#
# THEM BUOC MOI: chi them dong vao STEPS ben duoi, khong can sua gi khac.

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# Khi chay qua "sudo -u ...", sudo reset LANG/LC_ALL => input() loi
# UnicodeDecodeError. Ep stdin/stdout ve UTF-8. line_buffering: in ngay tung
# dong (khi chay qua pipe/nohup/tee, neu khong tieu de "Buoc N" bi don xuong
# cuoi, sau output cua cac script con).
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        if _stream is sys.stdin:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        else:
            _stream.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

SCRIPTS_DIR = Path(__file__).resolve().parent

# ===== SUA CAC THONG SO NAY CHO DUNG MOI TRUONG =====
# Tu chon theo he dieu hanh: Windows = may dev local (da test), Linux = server.
if os.name == 'nt':
    ODOO_ROOT = Path(r'E:\Odoo\odoo_19_community')
    ODOO_CONF = ODOO_ROOT / 'odoo.conf'
    DB_NAME = 'eaut_library_migrate'
    # conf local chua co eaut_library trong addons_path -> truyen kem. De '' neu
    # file conf da du addons_path.
    ADDONS_PATH = ','.join([
        str(ODOO_ROOT / 'odoo-19.0' / 'odoo' / 'addons'),
        str(ODOO_ROOT / 'odoo-19.0' / 'addons'),
        str(ODOO_ROOT / 'extra_addons'),          # vd backend_theme_pro
        r'E:\Odoo\odoo_18_educat\eaut_library',
    ])
    SOURCE_ODOO_CONF = Path(r'E:\Odoo\odoo_18_educat\odoo.conf')
    SOURCE_DB = 'eaut_library_backup'
    SOURCE_FILESTORE = Path(r'E:\Odoo\backup_library\odoo_eaut_lib_v18_filestore_backup_2026-11-27_00-15-01')
    DATA_DIR = Path(r'E:\Odoo\backup_library\migration_data')
else:
    ODOO_ROOT = Path('/opt/odoo_library_v19')                      # SUA: thu muc Odoo 19 tren server
    ODOO_CONF = Path('/etc/odoo/odoo_library_v19.conf')            # SUA: conf Odoo 19 (co eaut_library)
    DB_NAME = 'odoo_library_v19'                                   # SUA: DB dich
    ADDONS_PATH = ''                                               # '' = dung addons_path trong conf
    SOURCE_ODOO_CONF = Path('/etc/odoo/odoo_eaut_lib_v18.conf')    # SUA: conf Odoo 18 (DB nguon)
    SOURCE_DB = 'odoo_eaut_lib_v18'                                # SUA: DB nguon
    SOURCE_FILESTORE = Path('/opt/odoo/.local/share/Odoo/filestore/odoo_eaut_lib_v18')  # SUA
    DATA_DIR = Path('/opt/odoo_library_v19/migration_data')        # file JSON trung gian
ODOO_BIN = ODOO_ROOT / 'odoo-19.0' / 'odoo-bin'
# =====================================================


def export_args():
    return ['--odoo-conf', str(SOURCE_ODOO_CONF), '--db', SOURCE_DB, '--filestore', str(SOURCE_FILESTORE)]


# kind:
#   'install' - cai module Odoo 19 (+ ngon ngu) qua odoo-bin -i ... --stop-after-init
#   'python'  - script export: python thuong (chi can psycopg2), doc DB nguon
#   'shell'   - script import: "odoo-bin shell < script" tren DB dich; 'env' = bien
#               moi truong tro toi file JSON do buoc export tao ra
#   'delete'  - xoa file trung gian (vd file co hash mat khau)
STEPS = [
    {'module': 'setup', 'kind': 'install', 'odoo_module': 'library,website', 'lang': 'vi_VN',
     'label': 'Cai library + library_student + website, kich hoat tieng Viet (bo qua neu da cai)'},

    {'module': 'library', 'kind': 'python', 'script': 'library/export_from_restored_db.py',
     'args': lambda: export_args() + ['--output', str(DATA_DIR / 'library')],
     'label': 'Thu vien - xuat sach/ban online/don vi tai lieu/doc gia/the/phieu muon'},
    {'module': 'library', 'kind': 'shell', 'script': 'library/import_to_library.py',
     'env': lambda: {'LIBRARY_EXPORT_JSON': str(DATA_DIR / 'library' / 'library_export.json')},
     'label': 'Thu vien - nhap vao eaut_library (kem doi soat)'},

    # users chay SAU library: tai khoan sinh vien gan vao contact cua doc gia
    # (external id library_migration.member_*) do buoc library tao.
    {'module': 'users', 'kind': 'python', 'script': 'users/export_from_restored_db.py',
     'args': lambda: export_args() + ['--output', str(DATA_DIR / 'users')],
     'label': 'Tai khoan - xuat internal + portal user (kem hash mat khau)'},
    {'module': 'users', 'kind': 'shell', 'script': 'users/import_to_users.py',
     'env': lambda: {'USERS_EXPORT_JSON': str(DATA_DIR / 'users' / 'users_export.json')},
     'label': 'Tai khoan - nhap vao Odoo 19, giu nguyen mat khau cu'},
    {'module': 'users', 'kind': 'delete', 'path': lambda: DATA_DIR / 'users' / 'users_export.json',
     'label': 'Tai khoan - xoa file JSON co hash mat khau'},

    {'module': 'website', 'kind': 'python', 'script': 'website/export_from_restored_db.py',
     'args': lambda: export_args() + ['--output', str(DATA_DIR / 'website')],
     'label': 'Website - xuat trang/view/menu/anh/logo/bang mau (website co ten mien)'},
    {'module': 'website', 'kind': 'shell', 'script': 'website/import_to_website.py',
     'env': lambda: {'WEBSITE_EXPORT_JSON': str(DATA_DIR / 'website' / 'website_export.json')},
     'label': 'Website - nhap vao Odoo 19'},
]


def odoo_args(command=None):
    """[python, odoo-bin, (lenh con), -c conf, -d db, --no-http, (--addons-path)]"""
    args = [sys.executable, str(ODOO_BIN)] + ([command] if command else [])
    args += ['-c', str(ODOO_CONF), '-d', DB_NAME, '--no-http']
    if ADDONS_PATH:
        args.append('--addons-path=%s' % ADDONS_PATH)
    return args


def utf8_env(extra=None):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env.update(extra or {})
    return env


def run_install(step):
    args = odoo_args() + ['-i', step['odoo_module'], '--stop-after-init']
    if step.get('lang'):
        args.append('--load-language=%s' % step['lang'])
    return subprocess.run(args, cwd=str(ODOO_ROOT), env=utf8_env()).returncode


def run_python(step):
    args = [sys.executable, str(SCRIPTS_DIR / step['script'])] + step['args']()
    return subprocess.run(args, cwd=str(SCRIPTS_DIR), env=utf8_env()).returncode


def run_shell(step):
    with open(SCRIPTS_DIR / step['script'], 'rb') as stdin_file:
        return subprocess.run(odoo_args('shell'), stdin=stdin_file, cwd=str(ODOO_ROOT),
                              env=utf8_env(step['env']())).returncode


def run_delete(step):
    path = step['path']()
    if path.exists():
        path.unlink()
        print("Da xoa %s" % path)
    else:
        print("Khong co file %s (bo qua)" % path)
    return 0


RUNNERS = {'install': run_install, 'python': run_python, 'shell': run_shell, 'delete': run_delete}


def main():
    global DB_NAME, ODOO_CONF, SOURCE_DB, SOURCE_ODOO_CONF, SOURCE_FILESTORE, DATA_DIR

    parser = argparse.ArgumentParser(description='Chay toan bo pipeline migrate thu vien.')
    parser.add_argument('--list', action='store_true', help='Chi liet ke cac buoc, khong chay')
    parser.add_argument('--only', help='Chi chay 1 nhom buoc: setup | library | users | website')
    parser.add_argument('--from', dest='from_step', type=int, default=1, help='Chay tiep tu buoc so N')
    parser.add_argument('--db', help='Ghi de DB dich (Odoo 19)')
    parser.add_argument('--conf', help='Ghi de file conf Odoo 19')
    parser.add_argument('--source-db', help='Ghi de DB nguon (OpenEduCat)')
    parser.add_argument('--source-conf', help='Ghi de file conf Odoo 18')
    parser.add_argument('--filestore', help='Ghi de filestore cua DB nguon')
    parser.add_argument('--data-dir', help='Ghi de thu muc file JSON trung gian')
    parser.add_argument('--yes', action='store_true', help='Khong hoi xac nhan truoc khi chay')
    args = parser.parse_args()

    DB_NAME = args.db or DB_NAME
    ODOO_CONF = Path(args.conf) if args.conf else ODOO_CONF
    SOURCE_DB = args.source_db or SOURCE_DB
    SOURCE_ODOO_CONF = Path(args.source_conf) if args.source_conf else SOURCE_ODOO_CONF
    SOURCE_FILESTORE = Path(args.filestore) if args.filestore else SOURCE_FILESTORE
    DATA_DIR = Path(args.data_dir) if args.data_dir else DATA_DIR

    # Danh so theo vi tri trong STEPS de "--from N" luon chi dung 1 buoc, ke ca khi loc --only
    numbered = list(enumerate(STEPS, start=1))
    if args.only:
        numbered = [(i, s) for i, s in numbered if s['module'] == args.only]
        if not numbered:
            print("Khong co nhom '%s'. Cac nhom: %s" % (args.only, ', '.join(dict.fromkeys(s['module'] for s in STEPS))))
            sys.exit(1)
    numbered = [(i, s) for i, s in numbered if i >= args.from_step]
    if not numbered:
        print("Khong con buoc nao de chay (--from vuot qua so buoc).")
        return

    if args.list:
        for i, step in numbered:
            print("%2d. [%s] %s" % (i, step['module'], step['label']))
        return

    print("DB NGUON : %s (conf: %s)" % (SOURCE_DB, SOURCE_ODOO_CONF))
    print("Filestore: %s" % SOURCE_FILESTORE)
    print("DB DICH  : %s (conf: %s)" % (DB_NAME, ODOO_CONF))
    print("JSON tam : %s" % DATA_DIR)
    print("Se chay %d buoc:" % len(numbered))
    for i, step in numbered:
        print("  %2d. [%s] %s" % (i, step['module'], step['label']))

    problems = [p for p in (ODOO_BIN, ODOO_CONF, SOURCE_ODOO_CONF, SOURCE_FILESTORE) if not Path(p).exists()]
    if problems:
        print("\nKHONG TIM THAY: %s\nSua thong so o dau file run_all.py hoac dung tham so dong lenh." %
              ', '.join(map(str, problems)))
        sys.exit(1)

    if not args.yes:
        answer = input("\nXac nhan chay tren DB dich '%s'? (yes/no): " % DB_NAME).strip().lower()
        if answer not in ('y', 'yes'):
            print("Da huy.")
            return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    total_start = time.time()
    for i, step in numbered:
        print("\n===== Buoc %d/%d [%s]: %s =====" % (i, len(STEPS), step['module'], step['label']))
        t0 = time.time()
        code = RUNNERS[step['kind']](step)
        elapsed = time.time() - t0
        if code != 0:
            print("\n!!! Buoc %d THAT BAI (exit code %d) sau %.0fs. Dung lai." % (i, code, elapsed))
            print("Sau khi sua loi, chay lai voi: --from %d" % i)
            sys.exit(code)
        print("--- Buoc %d xong sau %.0fs ---" % (i, elapsed))

    print("\nDa chay xong %d buoc sau %.0f phut." % (len(numbered), (time.time() - total_start) / 60))


if __name__ == '__main__':
    main()
