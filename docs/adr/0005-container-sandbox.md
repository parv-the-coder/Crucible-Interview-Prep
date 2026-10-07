# ADR-0005: Docker containers for code execution, behind an interface

Status: Accepted
Date: 2026-08-23

## Context

The platform executes arbitrary code written by untrusted people. This is the
single highest-risk component: a failure here is not a bug, it is a breach.

Assume the code is actively hostile. It will try to read the filesystem,
open network connections, exhaust memory, fork without limit, and escape.

## Options considered

### Option A: Subprocess with rlimits, no namespaces
**Pros.** Trivial. No daemon. Milliseconds to start.
**Cons.** No filesystem isolation, code reads `.env` and the source tree. No
network isolation. rlimits are per-process, so a fork bomb evades `RLIMIT_AS`
entirely. Unsuitable for anything but local development.

### Option B: Docker containers
**Pros.** Kernel namespaces for filesystem, network, PID and users. cgroups for
memory and CPU. Capability dropping and seccomp. Widely deployed, widely
understood, easy to run locally.
**Cons.** Shares the host kernel, a kernel exploit escapes. Container start is
300 to 800 ms, which is significant against a 10-second budget. Requires a daemon,
and access to that daemon's socket is equivalent to root on the host.

### Option C, gVisor (`runsc`)
**Pros.** Intercepts syscalls in userspace, so a kernel bug is far harder to
reach. This is what Google runs untrusted code on.
**Cons.** 10 to 30% syscall overhead. Some syscalls unimplemented, which breaks
runtimes in ways that are hard to diagnose. Cannot run on the machines this
project is developed on.

### Option D: Firecracker microVMs
**Pros.** True hardware virtualisation. The strongest isolation available.
**Cons.** ~125 ms boot plus a kernel and rootfs to manage. Needs KVM, so it does
not run inside most CI or on a laptop VM. Substantially more machinery.

## Decision

**Option B for now, behind `SandboxBackend`**, an abstract interface that
`DockerSandbox` and `SubprocessSandbox` both implement, and that `GvisorSandbox`
or `FirecrackerSandbox` could implement later without any caller changing.

The interface is the actual decision. Docker is the current answer to a
question whose right answer changes with scale and threat model, so the code is
structured to make that swap a one-function change (`build_sandbox()`).

The subprocess backend exists solely so the platform runs on a machine without
a container runtime. It is explicit about being unsafe:
`capabilities.production_safe` is `False`, the readiness endpoint surfaces it,
and constructing it outside `local`/`test` raises. Critically, Docker being
unavailable in production does **not** silently fall back, turning "Docker is
down" into "we are now running untrusted code unconfined" is a far worse
failure than an outage.

Full details of the hardening: [06-sandbox-deep-dive.md](../06-sandbox-deep-dive.md).

## Consequences

### What this makes easy
- Strong isolation on commodity infrastructure, runnable on a laptop.
- Swapping to gVisor later is one function.
- Testing against a fake backend needs no daemon.

### What this makes hard
- Container start latency, addressed by pooling (ADR-0006).
- The daemon socket becomes a privileged dependency: whoever can reach it is effectively root on the host. In production the worker must be the only thing that can.

### What we accept
- **A kernel 0-day escapes this sandbox.** That is the honest limit of shared-kernel containment and no amount of capability dropping changes it. For genuinely adversarial multi-tenant load, this is where gVisor or a microVM becomes necessary.

## In short

> Docker, but the important part is it's behind an interface. Containers share
> the host kernel, so a kernel exploit escapes, no amount of cap-dropping
> fixes that. For genuinely hostile multi-tenant load you'd want gVisor or
> Firecracker. I structured it so that swap is one function, and I'd make it
> when the threat model justified the syscall overhead. What I explicitly
> refuse to do is fall back to the unsafe backend in production if Docker is
> unavailable, an outage is much better than silently running untrusted code
> unconfined.
