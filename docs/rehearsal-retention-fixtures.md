# Canonical positive retention fixture paths

RehearsalRetention creates its own temporary and evidence directories. Resolve
only those newly created directories before using them as positive fixtures.
On hosts with an aliased temporary root, the raw spelling can have symlink
ancestry even though the created directory is private and owned by this user.
NativeRetention correctly refuses such ancestry in a supplied destination.

Before any native call, retain the raw and canonical spellings, stat/lstat
identity, owner, mode and observed symlink ancestry in a private fixture sidecar.
Check that canonicalization retained the same device/inode and that the resulting
directories are owned, private directories with no symlink ancestry. The sidecar
uses a separate suffix so evidence-off and native packet assertions retain their
original meaning. An external BRAIN_REHEARSAL_EVIDENCE_DIR keeps the sidecar after
fixture cleanup; without that opt-in it follows the disposable fixture lifetime.

Caller-supplied destinations and deliberate invalid-destination controls are not
normalized. Production ancestry refusals, persistence, deletion, permissions and
all existing positive and negative assertions remain unchanged. This addresses
the source-visible fixture portability gap; it does not establish the cause of
an earlier run whose actual destination and rejection predicate were not saved.
