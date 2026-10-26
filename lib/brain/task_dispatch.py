"""Explicit prepared-task launch authority shared by CLI and native maintenance."""
import re

from . import jobs, task_flow, tasks


def contract_authority(n, row):
    if not re.fullmatch(r'(?:repo:D[0-9]+|private:D-[A-Za-z0-9-]+)', row.get('authority', '')):
        raise ValueError('task launch requires its explicit decision authority')
    from pathlib import Path
    relative = row.get('contract', '')
    path = n['dir'] / relative
    if (not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or
            path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(n['dir'].resolve())):
        raise ValueError('task launch requires its node-local contract file')
    text = path.read_text()
    if jobs.contract_problems(text):
        raise ValueError('task launch contract is incomplete')
    from . import ui
    sections = {k.lower(): v for k, v in ui.md_sections(text).items()}
    reason = sections.get('why separate', '').strip()
    policy = n['fm'].get('separation_policy', 'warn')
    if policy not in ('warn', 'required') or policy == 'required' and not reason:
        raise ValueError('task launch requires its explicit separation reason')
    if row.get('completion_kind') == 'merge' and not row.get('scope'):
        raise ValueError('task launch requires explicit code scope')
    return text, reason


def validate_launch_job(inst, n, job):
    ident = job.get('Task')
    if not ident:
        if tasks.read_records(n):
            raise ValueError('native launch requires a task-bound job on this node')
        return
    rows = tasks.read_records(n)
    row = rows.get(ident)
    if (not row or row['status'] != 'in-progress' or row.get('job') != jobs.job_key(job)
            or row.get('branch') != job['Job']):
        raise ValueError('native launch requires its exact active task/job binding')
    if task_flow.holds(inst, n, row, exclude_task=ident) or row.get('unresolved_dependencies') or task_flow.blockers(n, row, rows):
        raise ValueError('task dispatch is held or dependencies are unfinished')
    text, _ = contract_authority(n, row)
    path = jobs.contract_file(n, job)
    if path.is_symlink() or not path.is_file() or path.read_text() != text:
        raise ValueError('prepared native job contract differs from current task contract')


def launch_task(inst, n, ident, rpc=None):
    from . import worker_lifecycle
    row = tasks.read_records(n).get(ident)
    matches = [job for job in jobs.read_jobs(n) or [] if not job.get('Ended') and job.get('Task') == ident]
    if not row or len(matches) != 1:
        raise ValueError('task launch requires one prepared native job; use launch-plan commands first')
    validate_launch_job(inst, n, matches[0])
    return worker_lifecycle.launch_worker(inst, n, jobs.job_key(matches[0]), rpc=rpc)


def cmd_launch_task(a):
    import json
    from . import common, host_rpc, nodes
    try:
        inst = nodes.load()
        print(json.dumps(launch_task(inst, inst.node(a.node), a.task_id)))
    except (OSError, ValueError, host_rpc.HostRPCError) as error:
        common.die(str(error))
