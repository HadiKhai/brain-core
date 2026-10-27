"""Decision wakes and process gate proofs using fabricated local fixtures."""
import contextlib
import datetime
import io
import hashlib
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from unittest import mock

import core_loader
from scenario import CORE, Scenario


class DecisionGate(Scenario):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        self.m = core_loader.SourceFileLoader('decision_fixture', str(CORE / 'bin/brain')).load_module()
        self.root = self.init('oak')
        self.b(self.root, 'set', 'root', 'wake=fixture-send', 'coordinator=fixture-root')
        self.inst = self.m.Instance(self.root)
        self.n = self.inst.node('root')

    def test_default_policy_and_explicit_kinds(self):
        items = [{'id': c * 64, 'kind': kind, 'node': 'root', 'text': text, **extra}
                 for c, kind, text, extra in (
                     ('a', 'unsettled-threads', 'Bookkeeping', {}),
                     ('b', 'inbox', 'Ordinary note', {}),
                     ('c', 'inbox', 'Choose <!-- host-question:fixture -->', {}),
                     ('d', 'handback', 'Result', {'state': 'done'}))]
        groups = {'root': {'node': self.n, 'command': 'fixture-send', 'items': items}}
        with mock.patch.object(self.m, 'watch_groups', return_value=groups), \
                mock.patch.object(self.m, 'watch_failures'), \
                mock.patch.object(self.m, 'request_wake', return_value=True) as request, \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.watch_run(self.inst)
            self.assertEqual({i['id'] for call in request.call_args_list for i in call.kwargs['items']},
                             {'c' * 64, 'd' * 64})
            request.reset_mock()
            self.m.watch_run(self.inst, kinds={'unsettled-threads'})
            self.assertEqual([i['id'] for i in request.call_args.kwargs['items']], ['a' * 64])

    def test_default_kind_matrix_and_long_marked_inbox(self):
        allowed = ('job-stall', 'blocked-overdue', 'restart', 'job-restart', 'rotate', 'overdue-question')
        excluded = ('job-landed', 'job-ready', 'merged-job', 'unsettled-threads', 'unrecorded-worktree',
                    'missing-worktree', 'core-changes', 'rule-activation-review', 'overlapping-inputs',
                    'machine-hold', 'idle-with-runnable-work', 'future-question')
        for kind in allowed + excluded:
            with self.subTest(kind=kind):
                self.assertEqual(self.m.watch_decision_item({'kind': kind}), kind in allowed)
        for state in ('question', 'blocked', 'done', 'failed', 'queued'):
            self.assertEqual(self.m.watch_decision_item({'kind': 'handback', 'state': state}),
                             state in ('question', 'blocked', 'done'))
        (self.root / 'inbox.md').write_text('- ' + 'x' * 500 + ' <!-- host-question:long -->\n')
        item = next(i for i in self.m.watch_items(self.inst, self.n) if i['kind'] == 'inbox')
        self.assertNotIn('host-question', item['text'])  # excerpts can truncate the marker
        self.assertTrue(self.m.watch_decision_item(item))

    def test_queued_bookkeeping_cannot_retry_but_questions_survive(self):
        folder = self.m.wake_directory(self.inst, self.n)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('a' * 64 + '.pending')
        refs = [{'id': 'b' * 64, 'kind': 'unsettled-threads'},
                {'id': 'c' * 64, 'kind': 'inbox'}]
        path.write_text(json.dumps({'items': refs}))
        self.assertTrue(self.m.wake_actionable_pending(self.inst, path))
        self.assertEqual(json.loads(path.read_text())['items'], [refs[1]])
        path.write_text(json.dumps({'items': refs[:1]}))
        self.assertFalse(self.m.wake_actionable_pending(self.inst, path))
        path.write_text('corrupt')
        self.assertTrue(self.m.wake_actionable_pending(self.inst, path))
        self.assertEqual(path.read_text(), 'corrupt')
        for damaged in ({'items': [{'id': 123, 'kind': 'merged-job'}]},
                        {'items': [{'id': 'bad', 'kind': 'inbox'}]}, {'items': 'bad'}):
            path.write_text(json.dumps(damaged))
            before = path.read_bytes()
            self.assertTrue(self.m.wake_actionable_pending(self.inst, path))
            self.assertEqual(path.read_bytes(), before)
        path.write_text(json.dumps({'items': refs[:1], 'explicit_kinds': True}))
        self.assertTrue(self.m.wake_actionable_pending(self.inst, path))
        self.assertEqual(json.loads(path.read_text())['explicit_kinds'], True)

    def start_gate(self, label, code, *args, node=None, cwd=None, env=None, new_session=False):
        p = subprocess.Popen([sys.executable, str(CORE / 'bin/brain'), 'gate-lock',
                              *(['--node', node] if node else []), '--label', label,
                              '--', sys.executable, '-u', '-c', code, *args], cwd=cwd or self.root,
                             env=env or self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=new_session)
        def cleanup():
            if new_session:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin can report EPERM for an already emptied group.
                    # Never suppress a refusal while our supervisor is alive.
                    if p.poll() is None:
                        raise
            elif p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
        self.addCleanup(cleanup)
        return p

    def line(self, stream):
        self.assertTrue(select.select([stream], [], [], 5)[0], 'process barrier timed out')
        return stream.readline().strip()

    def gate_path(self, node='root'):
        return self.root / '.brain/gates' / (hashlib.sha256(node.encode()).hexdigest() + '.lock')

    def test_waiting_heavy_holder_is_idle_then_running_with_same_node_exclusion(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        code = r"""import argparse,os,sys
sys.path.insert(0,sys.argv[1]+'/lib')
from unittest import mock
import brain.machine as m
reading={'load_per_cpu':[9,9,9], 'mem_free_gb':32, 'disk_free_gb':200, 'thermal':'normal'}
def pause(seconds):
 print('admission-barrier',file=sys.stderr,flush=True)
 sys.stdin.readline()
 reading['load_per_cpu']=[0,0,0]
wait=m.machine_wait
def admission(*args,**kwargs):
 return wait(*args,**kwargs,read=lambda:dict(reading),pause=pause)
with mock.patch.object(m,'machine_wait',side_effect=admission):
 m.cmd_heavy(argparse.Namespace(node=None))
print('running-barrier',file=sys.stderr,flush=True)
sys.stdin.readline()
"""
        first = self.start_gate('admission', code, str(CORE), node='maple')
        self.assertEqual(self.line(first.stderr), 'admission-barrier')
        inst = self.m.Instance(self.root)
        path = self.gate_path('maple')
        before = path.read_bytes()
        inode = path.stat().st_ino
        self.assertEqual(json.loads(before)['phase'], 'waiting-for-admission')
        self.assertEqual(self.m.open_jobs_anywhere(inst), [])
        self.assertEqual(path.read_bytes(), before)
        second = self.start_gate('second', "import sys; print('second-barrier'); sys.stdin.readline()", node='maple')
        self.assertIn('waiting for gate held by admission', self.line(second.stderr))
        self.assertEqual(select.select([second.stdout], [], [], 0)[0], [])
        first.stdin.write('\n')
        first.stdin.flush()
        self.assertEqual(self.line(first.stderr), 'running-barrier')
        self.assertEqual(json.loads(path.read_text())['phase'], 'running')
        self.assertIn(('maple', 'in-flight node gate'), self.m.open_jobs_anywhere(inst))
        self.assertEqual(path.stat().st_ino, inode)
        out, err = first.communicate('\n', timeout=5)
        self.assertIn('machine care: heavy held', out)
        self.assertIn('heavy work allowed', out)
        self.assertEqual(self.line(second.stdout), 'second-barrier')
        self.assertEqual(path.stat().st_ino, inode)
        self.assertIn(('maple', 'in-flight node gate'), self.m.open_jobs_anywhere(inst))
        third = self.start_gate('third', 'print(43)', node='maple')
        self.assertIn('waiting for gate held by second', self.line(third.stderr))
        second.communicate('\n', timeout=5)
        out, err = third.communicate(timeout=5)
        self.assertEqual((first.returncode, second.returncode, third.returncode), (0, 0, 0), err)
        self.assertEqual(out.strip(), '43')
        self.assertEqual(path.stat().st_ino, inode)

    def wrapper_driver(self, admission, command):
        """Run `gate-lock` itself with a patched admission, then exec `command CORE`."""
        return r"""import argparse,os,sys
sys.path.insert(0,sys.argv[1]+'/lib')
from unittest import mock
import brain.machine as m, brain.gate_lock as g
reading={'load_per_cpu':[9,9,9], 'mem_free_gb':32, 'disk_free_gb':200, 'thermal':'normal'}
wait=m.machine_wait
""" + admission + r"""
with mock.patch.object(m,'machine_wait',side_effect=admission):
 g.cmd_gate_lock(argparse.Namespace(node='maple', label='queued', command=['--',sys.executable,'-u','-c',%r,sys.argv[1]]))
""" % command

    def start_wrapper(self, code):
        # Unbuffered bytes: a text reader could buffer a second line past select().
        p = subprocess.Popen([sys.executable, '-u', '-c', code, str(CORE)], cwd=self.root, env=self.env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        def cleanup():
            if p.poll() is None:
                p.kill()
            p.communicate()
        self.addCleanup(cleanup)
        return p

    def test_wrapper_waits_for_admission_as_idle_holder_then_runs_busy_in_fifo_order(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        code = self.wrapper_driver(r"""def pause(seconds):
 print('admission-barrier',file=sys.stderr,flush=True)
 sys.stdin.readline()
 reading['load_per_cpu']=[0,0,0]
def admission(*args,**kwargs):
 return wait(*args,**kwargs,read=lambda:dict(reading),pause=pause)""",
            "import sys; print('command-ran',flush=True); sys.stdin.readline()")
        first = self.start_wrapper(code)
        # Admission reports go to stderr; the command's stdout stays its own.
        self.assertIn(b'machine care: heavy held', self.line(first.stderr))
        self.assertEqual(self.line(first.stderr), b'admission-barrier')
        inst = self.m.Instance(self.root)
        path = self.gate_path('maple')
        inode = path.stat().st_ino
        before = path.read_bytes()
        row = json.loads(before)
        self.assertEqual((row['phase'], row['label'], row['pid']), ('waiting-for-admission', 'queued', first.pid))
        self.assertEqual(self.m.open_jobs_anywhere(inst), [])  # a queued wrapper does not block upgrade
        self.assertEqual(path.read_bytes(), before)
        # The waiting wrapper still owns the node gate: a later run queues behind it.
        second = self.start_gate('second', "import sys; print('second-barrier'); sys.stdin.readline()", node='maple')
        self.assertIn('waiting for gate held by queued', self.line(second.stderr))
        self.assertEqual(select.select([second.stdout], [], [], 0)[0], [])
        first.stdin.write(b'\n')
        self.assertEqual(self.line(first.stdout), b'command-ran')
        self.assertEqual(json.loads(path.read_text())['phase'], 'starting')
        self.assertIn(('maple', 'in-flight node gate'), self.m.open_jobs_anywhere(inst))
        self.assertEqual(select.select([second.stdout], [], [], 0)[0], [])
        out, err = first.communicate(b'\n', timeout=5)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(out, b'')
        self.assertIn(b'machine care: heavy resumed', err)
        self.assertEqual(self.line(second.stdout), 'second-barrier')
        self.assertIn(('maple', 'in-flight node gate'), self.m.open_jobs_anywhere(inst))
        second.communicate('\n', timeout=5)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(path.stat().st_ino, inode)

    def test_wrapper_admission_leaves_command_its_own_first_idle_admission(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        command = r"""import sys
sys.path.insert(0,sys.argv[1]+'/lib')
from unittest import mock
import os, time, brain.machine as m, brain.nodes as nodes, brain.upgrade as u
inst=nodes.load()
def held(*args):
 print(m._m_gate_lock.gate_metadata(int(os.environ['BRAIN_GATE_FD']))['phase'], u.open_jobs_anywhere(inst), flush=True)
 return 'token'
with mock.patch.object(m,'machine_admit',side_effect=held):
 m.machine_wait(inst=inst, node='maple', token='token', clock=time)
"""
        first = self.start_wrapper(self.wrapper_driver('admission=wait', command))
        out, err = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, err)
        self.assertEqual(out.strip(), b'waiting-for-admission []')
        # The command's own admission made the gate running; it is sticky.
        self.assertEqual(json.loads(self.gate_path('maple').read_text())['phase'], 'running')

    def test_failed_wrapper_admission_releases_gate_without_running_command(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        code = self.wrapper_driver("def admission(*args,**kwargs):\n raise SystemExit('machine care: held')",
                                   "print('command-ran')")
        first = self.start_wrapper(code)
        out, err = first.communicate(timeout=10)
        self.assertNotEqual(first.returncode, 0)
        self.assertNotIn(b'command-ran', out)
        self.assertIn(b'machine care: held', err)
        self.assertEqual(json.loads(self.gate_path('maple').read_text())['phase'], 'released')
        after = self.start_gate('after', 'print(43)', node='maple')
        out, err = after.communicate(timeout=5)
        self.assertEqual((after.returncode, out.strip()), (0, '43'), err)
        self.assertNotIn('stale holder', err)

    def test_native_first_admission_is_idle_but_later_lane_admission_is_busy(self):
        m, inst, n = self.m, self.inst, self.n
        with m.node_gate(inst, n, self.env, 'native'):
            def wait():
                self.assertEqual(json.loads(self.gate_path().read_text())['phase'], 'waiting-for-admission')
                self.assertEqual(m.open_jobs_anywhere(inst), [])
                return 'fixture-token'
            with mock.patch.object(m, 'machine_admit', side_effect=lambda *a: wait()):
                self.assertEqual(m.machine_wait(inst=inst, node='root', token='token', clock=time), 'fixture-token')
            self.assertEqual(json.loads(self.gate_path().read_text())['phase'], 'running')
            def later(*args):
                self.assertIn(('root', 'in-flight node gate'), m.open_jobs_anywhere(inst))
                return 'later-token'
            with mock.patch.object(m, 'machine_admit', side_effect=later):
                self.assertEqual(m.machine_wait(inst=inst, node='root', token='token', clock=time), 'later-token')

    def test_failed_admission_restores_busy_phase_without_losing_gate(self):
        with self.m.node_gate(self.inst, self.n, self.env, 'failure'):
            with mock.patch.object(self.m, 'machine_admit', side_effect=RuntimeError('admission failed')):
                with self.assertRaisesRegex(RuntimeError, 'admission failed'):
                    self.m.machine_wait(inst=self.inst, node='root', token='token', clock=time)
            self.assertEqual(json.loads(self.gate_path().read_text())['phase'], 'running')
            self.assertIn(('root', 'in-flight node gate'), self.m.open_jobs_anywhere(self.inst))

    def test_suspended_outer_writer_scope_never_retains_lock_during_admission(self):
        import fcntl
        m, inst = self.m, self.inst
        def assert_writer_released():
            self.assertIsNone(m.unlock.__globals__['_LOCK']['fh'])
            with (self.root / '.brain/lock').open('r+') as probe:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Native readiness releases the physical lock while its CLI context's
        # logical depth remains nonzero until the command returns.
        with m.brain_lock(inst):
            m.unlock()
            with m.node_gate(inst, self.n, self.env, 'suspended-cli'):
                assert_writer_released()
                with m.gate_admission(inst, 'root'):
                    assert_writer_released()
                    self.assertEqual(m.open_jobs_anywhere(inst), [])
                assert_writer_released()
                self.assertIn(('root', 'in-flight node gate'), m.open_jobs_anywhere(inst))

    def assert_gate_released(self):
        row = json.loads(self.gate_path().read_text())
        self.assertEqual(row['phase'], 'released')
        self.assertNotIn('phase_owner', row)
        successor = self.start_gate('successor', 'print(42)')
        out, err = successor.communicate(timeout=5)
        self.assertEqual(successor.returncode, 0, err)
        self.assertEqual(out.strip(), '42')
        self.assertNotIn('stale holder', err)

    def test_cli_close_marks_normal_nonzero_and_signal_exits_released(self):
        for code, expected in (('pass', 0), ('raise SystemExit(7)', 7),
                               ('import os,signal; os.kill(os.getpid(),signal.SIGTERM)', -signal.SIGTERM),
                               ('import os,signal; os.kill(os.getpid(),signal.SIGKILL)', -signal.SIGKILL)):
            with self.subTest(code=code):
                child = self.start_gate('exit', code)
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, expected, err)
                self.assertNotIn('stale holder', err)
                self.assert_gate_released()
        child = self.start_gate('forwarded', "import sys; print('held'); sys.stdin.readline()")
        self.assertEqual(self.line(child.stdout), 'held')
        child.terminate()
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assert_gate_released()

    def test_group_interrupt_reaches_child_once_without_forwarding(self):
        code = """import os,signal,time
count=0
def interrupted(signum, frame):
 global count
 count+=1
signal.signal(signal.SIGINT,interrupted)
signal.signal(signal.SIGQUIT,interrupted)
print(os.getpgrp(),flush=True)
while not count:
 time.sleep(.01)
time.sleep(.25)
print(count,flush=True)
"""
        for signum in (signal.SIGINT, signal.SIGQUIT):
            with self.subTest(signal=signum):
                child = self.start_gate('group interrupt', code, new_session=True)
                self.assertEqual(int(self.line(child.stdout)), child.pid)
                os.killpg(child.pid, signum)
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(out.strip(), '1')
                self.assert_gate_released()

    def test_direct_interrupt_forwards_once_after_two_second_grace(self):
        code = """import signal,time,sys
count=0
def interrupted(signum, frame):
 global count
 count+=1
 print("received",flush=True)
signal.signal(signal.SIGINT,interrupted)
signal.signal(signal.SIGQUIT,interrupted)
print('ready',flush=True)
while not count:
 time.sleep(.01)
sys.stdin.readline()
print(count,flush=True)
"""
        for signum in (signal.SIGINT, signal.SIGQUIT):
            with self.subTest(signal=signum):
                child = self.start_gate('direct interrupt', code, new_session=True)
                self.assertEqual(self.line(child.stdout), 'ready')
                started = time.monotonic()
                child.send_signal(signum)
                # The child's output is a delivery acknowledgement. It must not
                # arrive before the grace, and must arrive once after it.
                self.assertEqual(select.select([child.stdout], [], [], 1.8)[0], [])
                self.assertEqual(self.line(child.stdout), 'received')
                self.assertGreaterEqual(time.monotonic() - started, 2.0)
                out, err = child.communicate('\n', timeout=5)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(out.strip(), '1')
                self.assert_gate_released()

    def test_direct_term_reaps_child_and_releases_gate_without_grace(self):
        child = self.start_gate('direct TERM', "import os,signal; print(os.getpid(),flush=True); signal.pause()",
                                new_session=True)
        pid = int(self.line(child.stdout))
        started = time.monotonic()
        child.terminate()
        out, err = child.communicate(timeout=1.8)
        self.assertEqual(child.returncode, -signal.SIGTERM, err)
        self.assertLess(time.monotonic() - started, 2.0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assert_gate_released()

    def test_cli_preserves_inherited_ignored_signals(self):
        setup = """import os,signal,sys
for signum in (signal.SIGHUP,signal.SIGINT,signal.SIGQUIT,signal.SIGTERM):
 signal.signal(signum,signal.SIG_IGN)
os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
"""
        code = """import os,signal
for signum in (signal.SIGHUP,signal.SIGINT,signal.SIGQUIT,signal.SIGTERM):
 assert signal.getsignal(signum)==signal.SIG_IGN
 os.kill(os.getpid(),signum)
print(42)
"""
        result = subprocess.run([sys.executable, '-c', setup, str(CORE / 'bin/brain'), 'gate-lock', '--',
                                 sys.executable, '-c', code], cwd=self.root, env=self.env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '42')
        self.assert_gate_released()

    def test_other_catchable_signals_forward_immediately_and_release(self):
        code = """import signal,sys
def handled(signum,frame):
 print('handled',flush=True)
 raise SystemExit(0)
signal.signal(int(sys.argv[1]),handled)
print('ready',flush=True)
while True:
 signal.pause()
"""
        for signum in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGALRM, signal.SIGVTALRM,
                       signal.SIGPROF, signal.SIGWINCH):
            with self.subTest(signal=signum):
                child = self.start_gate('other signal', code, str(int(signum)), new_session=True)
                self.assertEqual(self.line(child.stdout), 'ready')
                child.send_signal(signum)
                out, err = child.communicate(timeout=1.8)
                self.assertEqual(child.returncode, 0, err)
                self.assertEqual(out.strip(), 'handled')
                self.assert_gate_released()

    def test_cli_preserves_callers_inheritable_descriptors(self):
        with (self.root / 'caller-input').open('w+b') as stream:
            stream.write(b'caller descriptor')
            stream.seek(0)
            fd = stream.fileno()
            code = "import os,sys; print(os.read(int(sys.argv[1]),4096).decode())"
            result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'gate-lock', '--',
                                     sys.executable, '-c', code, str(fd)], cwd=self.root, env=self.env,
                                    pass_fds=(fd,), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'caller descriptor')
        self.assert_gate_released()

    def test_cli_failed_exec_marks_released_and_preserves_exit_code(self):
        for command, expected in ((str(self.root / 'missing-command'), 127), (str(self.root), 126)):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, str(CORE / 'bin/brain'), 'gate-lock', '--', command],
                                        cwd=self.root, env=self.env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn('gate-lock:', result.stderr)
                self.assert_gate_released()

    def test_nested_close_does_not_release_outer_metadata(self):
        code = """import os,subprocess,sys
fd=int(os.environ['BRAIN_GATE_FD'])
subprocess.run([sys.executable,sys.argv[1],'gate-lock','--',sys.executable,'-c','pass'],pass_fds=(fd,),check=True)
print('nested-finished',flush=True)
sys.stdin.readline()
"""
        child = self.start_gate('outer', code, str(CORE / 'bin/brain'))
        self.assertEqual(self.line(child.stdout), 'nested-finished')
        self.assertNotEqual(json.loads(self.gate_path().read_text())['phase'], 'released')
        child.communicate('\n', timeout=5)
        self.assert_gate_released()

    def test_owned_close_failure_still_closes_fd_and_context_setup_failure_releases(self):
        gate = self.m.node_gate.__wrapped__.__globals__
        fd, owned = self.m.gate_acquire(self.inst, 'root', self.env, 'failure')
        self.assertTrue(owned)
        with mock.patch.object(gate['os'], 'pwrite', side_effect=OSError('fixture failure')):
            gate['gate_close'](fd)
        with self.assertRaises(OSError):
            os.fstat(fd)
        with mock.patch.object(gate['_m_machine'], 'process_stamp', return_value='fixture'), \
                mock.patch.dict(gate, gate_environment=mock.Mock(side_effect=RuntimeError('setup failure'))):
            with self.assertRaisesRegex(RuntimeError, 'setup failure'):
                with self.m.node_gate(self.inst, self.n, self.env, 'setup'):
                    self.fail('must not yield')
        self.assertEqual(json.loads(self.gate_path().read_text())['phase'], 'released')

    def test_dead_holder_is_reported_and_reclaimed_without_replacing_inode(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'], env=self.env)
        dead.wait(timeout=5)
        path = self.gate_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(node='root', pid=dead.pid, phase='running')))
        before, inode = path.read_bytes(), path.stat().st_ino
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic):
            self.assertEqual(self.m.open_jobs_anywhere(self.inst), [])
        self.assertIn('stale node gate holder pid ' + str(dead.pid), diagnostic.getvalue())
        self.assertEqual(path.read_bytes(), before)
        next_gate = self.start_gate('reclaim', 'print(42)')
        out, err = next_gate.communicate(timeout=5)
        self.assertEqual(next_gate.returncode, 0, err)
        self.assertEqual(out.strip(), '42')
        self.assertIn('stale holder pid ' + str(dead.pid), err)
        self.assertEqual(path.stat().st_ino, inode)

    def test_dead_wait_reporter_and_overlapping_inherited_admissions_fail_closed(self):
        with self.m.node_gate(self.inst, self.n, self.env, 'overlap') as env:
            with self.m.gate_admission(self.inst, 'root'):
                self.assertEqual(self.m.open_jobs_anywhere(self.inst), [])
                # An inherited second admission cannot hide work from the first.
                with self.m.gate_admission(self.inst, 'root'):
                    self.assertIn(('root', 'in-flight node gate'), self.m.open_jobs_anywhere(self.inst))
            self.assertEqual(json.loads(self.gate_path().read_text())['phase'], 'running')
            fd = int(env['BRAIN_GATE_FD'])
            row = self.m.gate_metadata(fd)
            dead = subprocess.Popen([sys.executable, '-c', 'pass'], env=self.env)
            dead.wait(timeout=5)
            row.update(phase='waiting-for-admission', phase_owner=[dead.pid, 'dead'])
            self.m.gate_write_metadata(fd, row)
            self.assertIn(('root', 'in-flight node gate'), self.m.open_jobs_anywhere(self.inst))

    def test_scenario_isolates_and_restores_enclosing_native_gate_proof(self):
        code = r"""import os,sys,unittest
sys.path.insert(0,sys.argv[1]+'/tests')
sys.path.insert(0,sys.argv[1]+'/lib')
from pathlib import Path
from brain.gate_lock import GATE_ENV, gate_inherited_fd
from test_decision_gate import DecisionGate
# Reuse isolated setup initialization inside this already nested CLI probe.
DecisionGate.fixture_init_in_process = True
DecisionGate.fixture_templates = True
before={key:os.environ[key] for key in GATE_ENV}
fd=gate_inherited_fd(Path.cwd(),os.environ,'root')
identity=os.fstat(fd)
suite=unittest.TestSuite(DecisionGate(name) for name in (
    'test_different_nodes_overlap_without_label_identity',
    'test_native_lane_gate_stays_held_between_commands_and_closes_own_fd'))
result=unittest.TextTestRunner(stream=sys.stderr).run(suite)
if not result.wasSuccessful(): sys.exit(1)
assert {key:os.environ.get(key) for key in GATE_ENV} == before
assert gate_inherited_fd(Path.cwd(),os.environ,'root') == fd
assert os.fstat(fd).st_ino == identity.st_ino
print('fixture proof isolated; enclosing proof restored and still held')
"""
        outer = self.start_gate('enclosing native gate', code, str(CORE))
        out, err = outer.communicate(timeout=30)
        self.assertEqual(outer.returncode, 0, out + err)
        self.assertIn('enclosing proof restored and still held', out)

    def test_most_specific_contained_context_and_conflicting_registrations(self):
        home = self.repo(self.root / 'projects/maple')
        bench = self.root / 'projects/workbench'
        worktree = bench / 'worktrees/job'
        worktree.mkdir(parents=True)
        self.b(self.root, 'new', 'maple', '--title', 'Maple', '--home', str(home))
        self.b(self.root, 'new', 'elm', '--title', 'Elm')
        inst = self.m.Instance(self.root)
        # The same node may register the same anchor in several roles.
        inst.local_of('maple').update(workbench=str(bench),
            worktrees={'home-alias': {'path': str(home)}, 'job': {'path': str(worktree)}})
        inst.save_local()
        for cwd in (home, bench, worktree):
            with self.subTest(cwd=cwd):
                p = self.start_gate('contained context', "import os; print(os.environ['BRAIN_GATE_NODE'])", cwd=cwd)
                out, err = p.communicate(timeout=3)
                self.assertEqual(p.returncode, 0, err)
                self.assertEqual(out.strip(), 'maple')
        # A deeper node folder wins over the enclosing node's registered home.
        inst.local_of('maple')['workbench'] = str(self.root / 'nodes')
        inst.save_local()
        p = self.start_gate('node folder', "import os; print(os.environ['BRAIN_GATE_NODE'])", cwd=self.root / 'nodes/elm')
        out, err = p.communicate(timeout=3)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(out.strip(), 'elm')
        # Equal-depth node-folder/registration conflicts refuse, not root fallback.
        inst.local_of('maple')['workbench'] = str(self.root / 'nodes/elm')
        inst.save_local()
        bad = self.start_gate('tie', 'print(99)', cwd=self.root / 'nodes/elm')
        out, err = bad.communicate(timeout=3)
        self.assertNotEqual(bad.returncode, 0, out + err)
        self.assertIn('--node', err)
        explicit = self.start_gate('explicit wins', "import os; print(os.environ['BRAIN_GATE_NODE'])",
                                   node='maple', cwd=self.root / 'nodes/elm')
        out, err = explicit.communicate(timeout=3)
        self.assertEqual(explicit.returncode, 0, err)
        self.assertEqual(out.strip(), 'maple')

    def test_equal_contained_home_registrations_refuse(self):
        home = self.repo(self.root / 'projects/shared')
        for node in ('maple', 'elm'):
            self.b(self.root, 'new', node, '--title', node.title(), '--home', str(home))
        bad = self.start_gate('contained tie', 'print(99)', cwd=home)
        out, err = bad.communicate(timeout=3)
        self.assertNotEqual(bad.returncode, 0, out + err)
        self.assertIn('--node', err)
        self.assertNotIn('99', out)

    def test_different_nodes_overlap_without_label_identity(self):
        for node in ('maple', 'elm'):
            self.b(self.root, 'new', node, '--title', node.title())
        first = self.start_gate('identical label', "import sys; print('held'); sys.stdin.readline()",
                                cwd=self.root / 'nodes/maple')
        self.assertEqual(self.line(first.stdout), 'held')
        second = self.start_gate('identical label', "print('independent')", cwd=self.root / 'nodes/elm')
        out, err = second.communicate(timeout=3)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(out.strip(), 'independent')
        self.assertIsNone(first.poll())
        self.assertFalse((self.root / '.brain/gate.lock').exists())

    def test_explicit_node_and_context_share_stable_lock(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        first = self.start_gate('../../display only', "import sys; print('held'); sys.stdin.readline()",
                                node='maple')
        self.assertEqual(self.line(first.stdout), 'held')
        inode = self.gate_path('maple').stat().st_ino
        second = self.start_gate('another label', "print('released')", cwd=self.root / 'nodes/maple')
        self.assertIn('waiting for gate held by ../../display only', self.line(second.stderr))
        first.communicate('\n', timeout=3)
        out, err = second.communicate(timeout=3)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(out.strip(), 'released')
        self.assertEqual(self.gate_path('maple').stat().st_ino, inode)

    def test_inherited_wrong_node_path_and_unlocked_proof_refuse(self):
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        for mutation in ("args=['--node','maple']", "os.environ['BRAIN_GATE_PATH'] += '.stale'; args=[]",
                         "import fcntl; fcntl.flock(int(os.environ['BRAIN_GATE_FD']),fcntl.LOCK_UN); args=[]",
                         "os.environ.pop('BRAIN_GATE_FD'); args=[]"):
            with self.subTest(mutation=mutation):
                code = ("import os,sys; " + mutation + "; os.execv(sys.executable, "
                        "[sys.executable,sys.argv[1],'gate-lock',*args,'--',sys.executable,'-c','print(99)'])")
                outer = self.start_gate('outer', code, str(CORE / 'bin/brain'))
                out, err = outer.communicate(timeout=3)
                self.assertNotEqual(outer.returncode, 0, out + err)
                self.assertIn('inherited', err)
                self.assertNotIn('99', out)
        next_gate = self.start_gate('after refusals', 'print(42)')
        out, err = next_gate.communicate(timeout=3)
        self.assertEqual(next_gate.returncode, 0, err)
        self.assertEqual(out.strip(), '42')

    def test_external_context_requires_unique_registered_node(self):
        home = self.repo(self.tmp / 'home')
        for node in ('maple', 'elm'):
            self.b(self.root, 'new', node, '--title', node.title(), '--home', str(home))
        bad = self.start_gate('ambiguous', 'print(99)', cwd=home)
        out, err = bad.communicate(timeout=3)
        self.assertNotEqual(bad.returncode, 0, out + err)
        self.assertIn('--node', err)
        good = self.start_gate('explicit', 'print(42)', node='elm', cwd=home)
        out, err = good.communicate(timeout=3)
        self.assertEqual(good.returncode, 0, err)
        self.assertEqual(out.strip(), '42')

    def test_native_direct_excludes_own_node_before_machine_admission(self):
        home = self.repo(self.tmp / 'native-home')
        self.b(self.root, 'set', 'root', 'home=' + str(home))
        self.b(self.root, 'new', 'maple', '--title', 'Maple')
        fifo = self.tmp / 'direct-ready'
        os.mkfifo(fifo)
        read_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, read_fd)
        child = ("import os,sys; f=os.open(" + repr(str(fifo)) +
                 ",os.O_WRONLY); os.write(f,b'1'); os.close(f); sys.stdin.readline()")
        command = 'exec ' + shlex.join([sys.executable, '-u', '-c', child])
        code = ("import os,sys; sys.path.insert(0,sys.argv[1]+'/lib'); "
                "from brain.instance import Instance; from pathlib import Path; "
                "import brain.verify as v; inst=Instance(Path.cwd()); "
                "result=v.run_verify(inst,inst.node('root'),'fixture'," + repr(command) +
                ",{'path':os.getcwd()},dict(os.environ)); sys.exit(result[0])")
        def native():
            p = subprocess.Popen([sys.executable, '-u', '-c', code, str(CORE)], cwd=self.root,
                                 env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
            def cleanup():
                try:
                    p.communicate(None if p.stdin.closed else '\n', timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.communicate()
            self.addCleanup(cleanup)
            return p
        first = native()
        self.assertTrue(self.line(first.stdout).startswith('verify log:'))
        self.assertTrue(select.select([read_fd], [], [], 5)[0], 'direct child not started')
        self.assertEqual(os.read(read_fd, 1), b'1')
        second = native()
        # Wait for an explicit lock-wait barrier, not an absence sampled during startup.
        self.assertIn('waiting for gate held by', self.line(second.stderr))
        self.assertEqual(select.select([second.stdout], [], [], 0)[0], [])
        queue = self.tmp / '.local/state/brain/machine-care-queue.json'
        self.assertEqual(len(json.loads(queue.read_text())), 1)
        independent = self.start_gate('independent', 'print(42)', node='maple')
        out, err = independent.communicate(timeout=3)
        self.assertEqual(independent.returncode, 0, err)
        first.communicate('\n', timeout=5)
        self.assertTrue(self.line(second.stdout).startswith('verify log:'))
        self.assertTrue(select.select([read_fd], [], [], 5)[0], 'second child not started')
        self.assertEqual(os.read(read_fd, 1), b'1')
        second.communicate('\n', timeout=5)
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(json.loads(queue.read_text()), [])

    def test_registered_home_workbench_and_worktree_infer_the_same_node(self):
        home = self.repo(self.tmp / 'registered-home')
        bench = self.tmp / 'registered-workbench'
        worktree = bench / 'worktrees/job'
        worktree.mkdir(parents=True)
        self.b(self.root, 'new', 'maple', '--title', 'Maple', '--home', str(home))
        inst = self.m.Instance(self.root)
        inst.local_of('maple').update(workbench=str(bench), worktrees={'job': {'path': str(worktree)}})
        inst.save_local()
        for cwd in (home, bench, worktree):
            with self.subTest(cwd=cwd):
                first = self.start_gate('explicit', "import sys; print('held'); sys.stdin.readline()", node='maple')
                self.assertEqual(self.line(first.stdout), 'held')
                second = self.start_gate('context', "import os; print(os.environ['BRAIN_GATE_NODE'])", cwd=cwd)
                self.assertIn('waiting for gate held by explicit', self.line(second.stderr))
                first.communicate('\n', timeout=3)
                out, err = second.communicate(timeout=3)
                self.assertEqual(second.returncode, 0, err)
                self.assertEqual(out.strip(), 'maple')
        env = {**self.env, 'BRAIN_ROOT': str(self.root)}
        outside = self.start_gate('unbound', 'print(99)', cwd=self.tmp, env=env)
        out, err = outside.communicate(timeout=3)
        self.assertNotEqual(outside.returncode, 0, out + err)
        self.assertIn('--node', err)

    def test_lock_targets_refuse_links_and_nonregular_files(self):
        path = self.gate_path()
        path.parent.mkdir(parents=True)
        outside = self.tmp / 'outside-lock'
        outside.write_text('untouched')
        for kind in ('symlink', 'hardlink', 'fifo', 'directory-symlink'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    path.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, path)
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    path.parent.rmdir()
                    path.parent.symlink_to(self.tmp, target_is_directory=True)
                p = self.start_gate('unsafe', 'print(99)')
                out, err = p.communicate(timeout=3)
                self.assertNotEqual(p.returncode, 0, out + err)
                self.assertNotIn('99', out)
                self.assertEqual(outside.read_text(), 'untouched')
                if kind == 'directory-symlink':
                    path.parent.unlink()
                    path.parent.mkdir()
                    self.assertFalse((self.tmp / path.name).exists())
                else:
                    path.unlink()

    def test_native_lane_gate_stays_held_between_commands_and_closes_own_fd(self):
        home = self.repo(self.tmp / 'lane-home')
        self.b(self.root, 'set', 'root', 'home=' + str(home))
        inst = self.m.Instance(self.root)
        n = inst.node('root')
        lanes = [dict(name=name, command='true', schedule='job', requires=[], always=False)
                 for name in ('first', 'second')]
        snap = dict(lanes=lanes, hashes={l['name']: 'fixture' for l in lanes},
                    context='fixture', evidence_context='fixture-evidence',
                    map_hash='fixture', config_hash='fixture')
        stream, fds, competitors = self.m.run_verify_stream, [], []
        def observe(inst, n, branch, command, w, env, token, lane=None):
            fd = int(env['BRAIN_GATE_FD'])
            fds.append(fd)
            self.assertEqual(env['BRAIN_GATE_NODE'], 'root')
            if lane == 'first':
                competitor = self.start_gate('same node', 'print(42)')
                competitors.append(competitor)
                self.assertIn('waiting for gate held by lanes', self.line(competitor.stderr))
            self.assertEqual(select.select([competitors[0].stdout], [], [], 0)[0], [])
            return stream(inst, n, branch, command, w, env, token, lane=lane)
        with mock.patch.object(self.m, 'run_verify_stream', side_effect=observe):
            result = self.m.run_lanes(inst, n, 'lanes', {'path': str(home)}, self.env,
                                      snap, None, 'head', 'main', {})
        self.assertEqual(result['verify_result'], 0)
        self.assertEqual(len(fds), 2)
        self.assertEqual(fds[0], fds[1])
        with self.assertRaises(OSError):
            os.fstat(fds[0])
        out, err = competitors[0].communicate(timeout=3)
        self.assertEqual(competitors[0].returncode, 0, err)
        self.assertEqual(out.strip(), '42')
        with self.m.machine_queue() as queue:
            self.assertEqual(queue.rows, [])

    def test_gate_serializes_waits_preserves_argv_and_exit(self):
        first = self.start_gate('first', "import sys; print('held', flush=True); sys.stdin.readline()")
        self.assertEqual(self.line(first.stdout), 'held')
        inode = self.gate_path().stat().st_ino
        second = self.start_gate('second', "import sys; print(repr(sys.argv[1:]), flush=True); sys.exit(7)", 'a b', '--brain=child')
        self.assertIn('waiting for gate held by first since ', self.line(second.stderr))
        self.assertEqual(select.select([second.stdout], [], [], 0)[0], [])
        first.communicate('\n', timeout=5)
        out, err = second.communicate(timeout=5)
        self.assertEqual(second.returncode, 7, err)
        self.assertEqual(out.strip(), "['a b', '--brain=child']")
        self.assertEqual(self.gate_path().stat().st_ino, inode)

    def test_gate_closed_nested_descriptor_refuses_without_deadlock(self):
        outer = self.start_gate('outer',
            "import subprocess,sys; p=subprocess.run([sys.executable,sys.argv[1], 'gate-lock', '--', "
            "sys.executable, '-c', 'print(99)'], close_fds=True, capture_output=True, text=True, timeout=3); "
            "print(p.returncode); print(p.stderr)", str(CORE / 'bin/brain'))
        out, err = outer.communicate(timeout=5)
        self.assertEqual(outer.returncode, 0, err)
        self.assertIn('inherited descriptor closed', out)
        self.assertNotIn('99', out)

    def test_gate_nested_and_signal_lifetime(self):
        nested = self.start_gate('outer', "import os,sys; os.execv(sys.executable, [sys.executable, sys.argv[1], 'gate-lock', '--label', 'inner', '--', sys.executable, '-c', 'print(42)'])", str(CORE / 'bin/brain'))
        out, err = nested.communicate(timeout=5)
        self.assertEqual(nested.returncode, 0, err)
        self.assertEqual(out.strip(), '42')
        first = self.start_gate('signal', "import sys; print('held', flush=True); sys.stdin.readline()")
        self.assertEqual(self.line(first.stdout), 'held')
        first.send_signal(signal.SIGTERM)
        first.communicate(timeout=5)
        self.assertEqual(first.returncode, -signal.SIGTERM)
        second = self.start_gate('next', 'print(43)')
        out, err = second.communicate(timeout=5)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(out.strip(), '43')

    def native_gate(self, command):
        home = self.repo(self.tmp / 'source')
        self.b(self.root, 'new', 'fixture', '--title', 'Fixture', '--home', str(home))
        parent = ("import os,sys; sys.path.insert(0,sys.argv[2]+'/lib'); "
                  "from brain.instance import Instance; import brain.verify as v; "
                  "from unittest import mock; from pathlib import Path; inst=Instance(Path.cwd()); "
                  "patch=mock.patch.object(v._m_machine,'machine_child'); patch.start(); "
                  "result=v.run_verify_stream(inst,inst.node('fixture'),'fixture',sys.argv[1], "
                  "{'path':os.getcwd()},dict(os.environ),None); sys.exit(result[0])")
        return self.start_gate('native', parent, command, str(CORE), node='fixture')

    def test_native_verify_retains_gate_fd_for_nested_wrapper_and_bootstrap(self):
        command = shlex.join([sys.executable, str(CORE / 'bin/brain'), 'gate-lock', '--node', 'fixture', '--',
                              sys.executable, '-c', 'print(42)'])
        parent = self.native_gate(command)
        out, err = parent.communicate(timeout=5)
        self.assertEqual(parent.returncode, 0, out + err)

    def test_native_verify_parent_death_keeps_gate_child_locked(self):
        fifo = self.root / 'ready.fifo'
        os.mkfifo(fifo)
        read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, read_fd)
        child = ("import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY); "
                 "os.write(fd,str(os.getpid()).encode()); os.close(fd); sys.stdin.readline()")
        command = 'exec ' + shlex.join([sys.executable, '-u', '-c', child, str(fifo)])
        parent = self.native_gate(command)
        self.assertTrue(select.select([read_fd], [], [], 5)[0], 'native child readiness barrier')
        pid = int(os.read(read_fd, 128))
        def kill_child():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(kill_child)
        parent.kill()
        parent.wait(timeout=5)
        os.kill(pid, 0)
        competitor = self.start_gate('competitor', "print('competitor-entered',flush=True)", node='fixture')
        self.assertIn('waiting for gate held by native since ', self.line(competitor.stderr))
        self.assertEqual(select.select([competitor.stdout], [], [], 0)[0], [])
        kill_child()
        out, err = competitor.communicate(timeout=5)
        self.assertEqual(out.strip(), 'competitor-entered', err)

    def test_exec_restores_sigpipe_with_closed_output_pipe(self):
        command = ['/usr/bin/yes', 'fixture']
        def run(argv):
            p = subprocess.Popen(argv, cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.stdout.close()
            stderr = p.stderr.read()
            p.wait(timeout=5)
            p.stderr.close()
            return p.returncode, stderr
        plain = run(command)
        wrapped = run([sys.executable, str(CORE / 'bin/brain'), 'gate-lock', '--', *command])
        self.assertEqual(plain[0], -signal.SIGPIPE)
        self.assertEqual(wrapped, plain)

    def test_nested_actual_work_retains_lock_after_parent_death(self):
        fifo = self.root / 'native-ready'
        os.mkfifo(fifo)
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        child = ("import os,sys; gate=int(os.environ['BRAIN_GATE_FD']); "
                 "assert os.fstat(gate).st_ino == os.stat(os.environ['BRAIN_GATE_PATH']).st_ino; "
                 "fd=os.open(sys.argv[1],os.O_WRONLY); os.write(fd,str(os.getpid()).encode()); "
                 "os.close(fd); sys.stdin.readline()")
        command = 'exec ' + shlex.join([sys.executable, str(CORE / 'bin/brain'),
                  'gate-lock', '--node', 'fixture', '--label', 'nested', '--', sys.executable, '-u', '-c', child, str(fifo)])
        parent = self.native_gate(command)
        self.assertTrue(select.select([fd], [], [], 5)[0], 'nested native child never reached barrier')
        pid = int(os.read(fd, 128))
        def cleanup():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.addCleanup(cleanup)
        parent.kill()
        parent.wait(timeout=5)
        os.kill(pid, 0)
        other = self.start_gate('competitor', "print('entered',flush=True)", node='fixture')
        self.assertIn('waiting for gate held by native since ', self.line(other.stderr))
        self.assertEqual(select.select([other.stdout], [], [], 0)[0], [])
        cleanup()
        out, err = other.communicate(timeout=5)
        self.assertEqual(other.returncode, 0, err)
        self.assertEqual(out.strip(), 'entered')
        print('native -> nested wrapper -> live child: competitor blocked after parent death, released after child death')

    def test_failed_native_publication_never_executes_work(self):
        home = self.repo(self.tmp / 'source')
        self.b(self.root, 'new', 'fixture', '--title', 'Fixture', '--home', str(home))
        marker = self.root / 'must-not-start'
        command = shlex.join([sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()'])
        code = ("import os,sys; sys.path.insert(0,sys.argv[2]+'/lib'); "
                "from brain.instance import Instance; import brain.verify as v; "
                "from unittest import mock; from pathlib import Path; inst=Instance(Path.cwd()); "
                "patch=mock.patch.object(v._m_machine,'machine_child',side_effect=RuntimeError('fixture publication rejected')); patch.start(); "
                "v.run_verify_stream(inst,inst.node('fixture'),'fixture',sys.argv[1], {'path':os.getcwd()},dict(os.environ),None)")
        parent = self.start_gate('native', code, command, str(CORE), node='fixture')
        out, err = parent.communicate(timeout=5)
        self.assertNotEqual(parent.returncode, 0)
        self.assertIn('fixture publication rejected', err)
        self.assertFalse(marker.exists())
        other = self.start_gate('next', 'print(42)')
        out, err = other.communicate(timeout=5)
        self.assertEqual(other.returncode, 0, err)
        self.assertEqual(out.strip(), '42')
        self.assertFalse(marker.exists())
        print('native publication failure: no command execution, no leaked gate ownership')

    def test_durable_retry_and_reroute_preserve_decision_policy(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        for mode in ('bookkeeping', 'mixed', 'explicit'):
            with self.subTest(mode=mode):
                inst = self.m.Instance(self.root)
                child, root = inst.node('sample'), inst.node('root')
                inst.local_of('sample').update(coordinator='fixture-child', wake='fixture-send')
                inst.save_local()
                folder = self.m.wake_directory(inst, child)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (mode + '.pending')
                bookkeeping = {'id': 'a' * 64, 'kind': 'merged-job'}
                question = {'id': 'b' * 64, 'kind': 'inbox'}
                refs = [bookkeeping]
                if mode == 'mixed':
                    refs.append(question)
                if mode == 'explicit':
                    refs.append({'id': 'c' * 64, 'kind': 'unsettled-threads'})
                path.write_text(json.dumps({'items': refs, 'explicit_kinds': mode == 'explicit'}))
                inst.local_of('sample').pop('coordinator')
                inst.save_local()  # drain must discover the new root route
                with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                        mock.patch.object(self.m, 'wake_send', return_value=False) as send:
                    self.m.wake_drain(inst, child, once=True)
                    destination = self.m.wake_directory(inst, root) / path.name
                    if mode == 'bookkeeping':
                        self.assertFalse(path.exists())
                        self.assertFalse(destination.exists())
                        self.m.wake_drain(inst, root, once=True)
                        send.assert_not_called()
                        continue
                    self.assertFalse(path.exists())
                    expected = [question] if mode == 'mixed' else [bookkeeping]
                    value = json.loads(destination.read_text())
                    self.assertEqual(value['items'], expected)
                    self.assertEqual(value['explicit_kinds'], mode == 'explicit')
                    self.m.wake_drain(inst, root, once=True)
                    self.assertEqual(send.call_count, 1)
                    self.assertTrue(destination.exists())
                    send.return_value = True
                    self.m.wake_drain(inst, root, once=True)
                    self.assertEqual(send.call_count, 2)
                    self.assertFalse(destination.exists())
                    history = self.m.wake_history(self.m.wake_directory(inst, root))
                    self.assertEqual(history[-1]['items'], expected)


    def test_ambiguous_question_receipts_survive_retirement_retry_and_reroute(self):
        self.b(self.root, 'new', 'sample', '--title', 'Sample')
        cases = [(kind, reverse, enqueue) for kind, reverse, enqueue in (
            ('unknown', False, False), ('conflict', False, False),
            ('conflict', True, False), ('conflict', False, True), ('conflict', True, True))]
        for route in (False, True):
            for index, (kind, reverse, enqueue) in enumerate(cases):
                with self.subTest(route=route, kind=kind, reverse=reverse, enqueue=enqueue):
                    inst = self.m.Instance(self.root)
                    n = inst.node('sample' if route else 'root')
                    if route:
                        inst.local_of('sample').update(coordinator='fixture-child', wake='fixture-send')
                        inst.save_local()
                        inst = self.m.Instance(self.root)
                        n = inst.node('sample')
                    marker = 'case-' + str(route) + '-' + str(index)
                    (n['dir'] / 'inbox.md').write_text('- Choose a fabricated color <!-- host-question:' + marker + ' -->\n')
                    question = next(i for i in self.m.watch_items(inst, n) if i['kind'] == 'inbox')
                    # Exercise the retirement prefix even though the original question is direct.
                    identity = self.m.watch_digest(['due-episode'])[:16] + str(index) * 48
                    refs = [{'id': identity, 'kind': 'inbox'}]
                    if kind == 'unknown':
                        refs[0]['kind'] = 'future-question'
                    else:
                        refs.append({'id': identity, 'kind': 'merged-job'})
                        if reverse:
                            refs.reverse()
                    with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                            mock.patch.object(self.m, 'wake_send', return_value=False) as send:
                        self.assertTrue(self.m.request_wake(inst, n, question['reason'],
                                                           items=refs if enqueue else [question]))
                        path = next(self.m.wake_directory(inst, n).glob('*.pending'))
                        if not enqueue:
                            path.write_text(json.dumps({'items': refs}))
                        original = path.read_bytes()
                        self.assertEqual(json.loads(original)['items'], refs)
                        self.m.wake_retire_due_pending(path, set())
                        self.assertEqual(path.read_bytes(), original)
                        self.assertTrue(self.m.wake_actionable_pending(inst, path))
                        self.assertEqual(path.read_bytes(), original)
                        # Remove source evidence so transport must use the opaque fallback.
                        (n['dir'] / 'inbox.md').write_text('')
                        if route:
                            inst.local_of('sample').pop('coordinator')
                            inst.save_local()
                            self.m.wake_drain(inst, n, once=True)
                            self.assertFalse(path.exists())
                            path = self.m.wake_directory(inst, inst.node('root')) / path.name
                            self.assertEqual(path.read_bytes(), original)
                        self.m.wake_drain(inst, inst.node('root'), once=True)
                        self.assertEqual(send.call_count, 1)
                        self.assertEqual(path.read_bytes(), original)
                        self.assertNotIn('future-question', send.call_args.kwargs['reason'])
                        send.return_value = True
                        self.m.wake_drain(inst, inst.node('root'), once=True)
                        self.assertEqual(send.call_count, 2)
                        self.assertFalse(path.exists())
                        path.with_suffix('.sent').unlink()

    def test_reference_normalization_rejects_conflicting_pairs_in_both_orders(self):
        refs = [{'id': 'a' * 64, 'kind': 'inbox'}, {'id': 'a' * 64, 'kind': 'merged-job'}]
        for ordered in (refs, list(reversed(refs))):
            with self.assertRaisesRegex(ValueError, 'conflicting wake identity kinds'):
                self.m.wake_item_refs(ordered)
        self.assertEqual(self.m.wake_item_refs([refs[0], refs[0]]), [refs[0]])


    def test_conflicts_across_receipts_or_snapshot_cannot_block_transport(self):
        (self.root / 'inbox.md').write_text('- Choose color <!-- host-question:collision -->\n')
        question = next(i for i in self.m.watch_items(self.inst, self.n) if i['kind'] == 'inbox')
        conflicting = {'id': question['id'], 'kind': 'rotate'}
        with mock.patch.object(self.m, 'wake_start'), mock.patch.object(self.m, 'WAKE_INTERVAL', 0), \
                mock.patch.object(self.m, 'wake_send', return_value=True) as send:
            # One internally valid receipt conflicts with the current display snapshot.
            self.assertTrue(self.m.request_wake(self.inst, self.n, 'snapshot conflict', items=[conflicting]))
            self.m.wake_drain(self.inst, self.n, once=True)
            self.assertEqual(send.call_count, 1)
            # Two internally valid receipts conflict when normalized as a delivery batch.
            self.assertTrue(self.m.request_wake(self.inst, self.n, 'first conflict', items=[question]))
            self.assertTrue(self.m.request_wake(self.inst, self.n, 'second conflict', items=[conflicting]))
            self.m.wake_drain(self.inst, self.n, once=True)
            self.assertEqual(send.call_count, 2)
            self.assertFalse(list(self.m.wake_directory(self.inst, self.n).glob('*.pending')))
