import os
import sys
import runpy
import ctypes

def preload_dlls(virtual_env):
    clidriver = os.path.join(virtual_env, 'Lib', 'site-packages', 'clidriver')
    bin_dir = os.path.join(clidriver, 'bin')
    vc14_dir = os.path.join(bin_dir, 'amd64.VC14.CRT')

    dll_paths = []
    if os.path.isdir(bin_dir):
        for name in os.listdir(bin_dir):
            if name.lower().endswith('.dll'):
                dll_paths.append(os.path.join(bin_dir, name))
    if os.path.isdir(vc14_dir):
        for name in os.listdir(vc14_dir):
            if name.lower().endswith('.dll'):
                dll_paths.append(os.path.join(vc14_dir, name))

    # Try to load common DB2 DLLs first
    priority = ['db2app64.dll', 'db2osse64.dll', 'db2cli64.dll']
    ordered = []
    for p in priority:
        for d in dll_paths:
            if os.path.basename(d).lower() == p.lower():
                ordered.append(d)
                break
    for d in dll_paths:
        if d not in ordered:
            ordered.append(d)

    for d in ordered:
        try:
            ctypes.CDLL(d)
            # print(f"Preloaded {d}")
        except Exception:
            # ignore failures; loader may still succeed when importing extension
            pass


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python -m scripts.preload_and_run <module> [args...]')
        sys.exit(2)

    virtual_env = os.environ.get('VIRTUAL_ENV')
    if not virtual_env or not os.path.isdir(virtual_env):
        print('Virtual environment not found; ensure VIRTUAL_ENV is set to the venv path')
        sys.exit(1)

    preload_dlls(virtual_env)

    module = sys.argv[1]
    # run remaining args as module args via setting sys.argv
    sys.argv = [module] + sys.argv[2:]
    # execute the module as __main__
    runpy.run_module(module, run_name='__main__')
