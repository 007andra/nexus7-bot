"""Run each suite in a clean process, allowing loopback mocks only.

Usage: python -m tests.run_offline [tests.test_module ...]
No exchange credentials or production environment are inherited.
"""
import concurrent.futures
import ipaddress
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import sysconfig
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def install_network_guard():
    def audit(event, args):
        if event == 'socket.connect':
            address = args[1]
            if not isinstance(address, tuple):
                raise PermissionError('offline tests: non-IP socket blocked')
            host = address[0]
        elif event == 'socket.getaddrinfo':
            host = args[0]
        else:
            return
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise PermissionError('offline tests: external network blocked')
    sys.addaudithook(audit)


def _function_test_names(module):
    """Return module-level test_* callables missed by unittest discovery."""
    return sorted(
        name for name, value in vars(module).items()
        if name.startswith('test_') and callable(value)
    )


def run_one(module):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'TEST_POSTGRES_DSN') if k in os.environ}
    # -S prevents the repository's production sitecustomize from importing
    # trading modules before each suite can install its loopback mock and mode.
    # Add the active interpreter's dependencies explicitly because -S also
    # skips automatic site-packages discovery.
    child_path = os.pathsep.join((str(ROOT), sysconfig.get_paths()['purelib']))
    env.update(PYTHONPATH=child_path, PAPER_TRADE='true', LOG_LEVEL='ERROR',
               KUCOIN_REST_BASE='http://127.0.0.1:1', NEXUS_TELEGRAM='false')
    try:
        with tempfile.TemporaryDirectory(prefix='nexus-test-') as cwd:
            proc = subprocess.run(
                [sys.executable, '-S', '-m', 'tests.run_offline', '--child', module],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=240,
            )
    except subprocess.TimeoutExpired as exc:
        def _as_text(value):
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else (value or '')
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
        return module, 124, 0, output + f'\nTIMEOUT: {module} exceeded 240s\n'
    output = proc.stdout + proc.stderr
    count = re.findall(r'(?:PASSOU|PASSARAM|PASS|Passou):\s*(\d+)', output)
    unit = re.search(r'Ran (\d+) tests?', output)
    return module, proc.returncode, int(count[-1]) if count else (int(unit[1]) if unit else 0), output


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        module_name = sys.argv[2]
        sys.argv = [module_name]
        install_network_guard()
        loaded = __import__(module_name, fromlist=['*'])
        suite = unittest.defaultTestLoader.loadTestsFromModule(loaded)
        unit_count = suite.countTestCases()
        function_names = _function_test_names(loaded)

        if unit_count:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            if not result.wasSuccessful():
                sys.exit(1)

        function_failures = 0
        for name in function_names:
            try:
                getattr(loaded, name)()
                print(f'{name} ... ok', flush=True)
            except Exception:
                function_failures += 1
                print(f'{name} ... FAIL', flush=True)
                import traceback
                traceback.print_exc()

        executed = unit_count + len(function_names)
        if executed:
            print(f'OFFLINE_TESTS_EXECUTED: {executed}', flush=True)
            sys.exit(bool(function_failures))

        # Legacy script-style suites are allowed only when they actually report
        # a non-zero test count. A silent import / PASS(0) is a CI failure.
        runpy.run_module(module_name, run_name='__main__', alter_sys=True)
    else:
        modules = sys.argv[1:] or ['tests.' + p.stem for p in sorted((ROOT/'tests').glob('test_*.py'))]
        failed = 0
        total = 0
        passed = 0
        skipped = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for module, rc, count, output in pool.map(run_one, modules):
                explicit = re.findall(r'OFFLINE_TESTS_EXECUTED:\s*(\d+)', output)
                if explicit:
                    count = int(explicit[-1])
                if rc == 0 and count == 0:
                    rc = 3
                    output += f'\nZERO-TEST FAILURE: {module} completed without executing any tests\n'
                child_skips = [int(x) for x in re.findall(r'skipped=(\\d+)', output, flags=re.IGNORECASE)]
                module_skipped = sum(child_skips)
                skipped += module_skipped
                print(f'{module}: {"PASS" if rc == 0 else "FAIL"} ({count})', flush=True)
                # Proof-only observability: surface named XRP proof evidence even
                # when the parent suite succeeds and normally suppresses child output.
                if module == 'tests.test_protection_readiness_authority':
                    for line in output.splitlines():
                        if (
                            'test_xrp_' in line
                            or line.startswith('[XRP_PROOF]')
                            or line.startswith('case=')
                            or line.startswith('result=')
                            or line.startswith('sol_unprotected_block=')
                            or line.startswith('next_gate=')
                            or line.startswith('blocker=')
                            or line.startswith('exchange_entry_mutations=')
                            or line.startswith('unknown_phase_')
                            or line.startswith('post_reconciliation_')
                        ):
                            print(line, flush=True)
                total += count
                passed += max(0, count - module_skipped) if rc == 0 else 0
                if rc:
                    failed += 1
                    print(output, flush=True)
        mandatory_skipped = skipped
        print(
            f'TOTAL={total} PASSED={passed} FAILED={failed} SKIPPED={skipped} '
            f'MANDATORY_SKIPPED={mandatory_skipped} FAILED_SUITES={failed}',
            flush=True,
        )
        sys.exit(bool(failed))
