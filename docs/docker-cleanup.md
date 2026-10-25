# Docker test cleanup

Cleanup is disabled unless a node declares all three settings:

```
docker_cleanup_prefixes: demo-verify-*,demo-job-*
docker_cleanup_host: unix:///var/run/docker.sock
docker_cleanup_daemon: your-trusted-local-daemon-id
```

Choose the endpoint and daemon ID from a trusted local Docker installation.
A Unix socket alone is insufficient proof of a local daemon; the explicit pin is
an administrative trust decision. A changed daemon requires updating the pin.

Label each disposable test container with `brain.docker-scope` (the two native
machine-scope hashes joined by `:`), `brain.owner-pid` and `brain.owner-stamp`
(the native process birth identity). Do not add these labels to real data.
The cleanup hook retains resources with missing/unknown ownership, a live or
recycled owner PID, bind mounts, preview/dev identity, a foreign connected peer,
or age under 30 minutes. Namespaces alone do not authorize stopping resources.
The hook's default action is stop; the leftover diagnostic remains read-only.

Run-close and due integrations call `docker_cleanup_nodes(instance, nodes)`.
Its result contains exact `stopped` and `removed` IDs and `errors`. Callers keep
their own primary outcome. Cleanup never invokes a host thread lifecycle API.

To remove unused owned resources as well, set `docker_cleanup_mode: remove`.
Volumes and networks must carry the same scope, project and owner labels as their
containers. No volume flag accompanies container removal. Every current container
reference protects a volume or network, and Docker's in-use refusal is honored.
Unlabeled, shared, attached, recent or uncertain resources remain for inspection.
