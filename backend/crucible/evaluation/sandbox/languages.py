"""Language execution profiles.

Each profile describes how to turn a source string into a running process
inside a container. Compiled languages get a separate compile step so that a
compile error is reported as a compile error rather than as "every test case
failed", which is what the v1 implementation did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Workspace layout inside the container. The rootfs is mounted read-only, so
# these are the only writable paths, and they are tmpfs (RAM), never disk.
WORKDIR = "/box"
SOURCE_DIR = f"{WORKDIR}/src"


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    id: str
    display_name: str
    image: str
    source_filename: str
    # argv (never a shell string) used to run the program.
    run_cmd: list[str]
    # argv used to compile; None for interpreted languages.
    compile_cmd: list[str] | None = None
    # Compiled artefacts need an exec-able mount; interpreted code does not,
    # so we can mount its workspace noexec and shrink the attack surface.
    needs_exec_mount: bool = False
    version_cmd: list[str] = field(default_factory=list)
    # Extra env inside the sandbox. Kept minimal on purpose.
    env: dict[str, str] = field(default_factory=dict)
    comment_prefix: str = "#"


PROFILES: dict[str, LanguageProfile] = {
    "python": LanguageProfile(
        id="python",
        display_name="Python 3.12",
        image="python:3.12-alpine",
        source_filename="main.py",
        # -I  isolated mode: ignores PYTHON* env vars and the cwd on sys.path,
        #     so a file named e.g. random.py in the workspace cannot hijack an
        #     import. -B stops .pyc writes onto a read-only mount.
        run_cmd=["python3", "-I", "-B", f"{SOURCE_DIR}/main.py"],
        version_cmd=["python3", "--version"],
        env={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        comment_prefix="#",
    ),
    "javascript": LanguageProfile(
        id="javascript",
        display_name="Node.js 20",
        image="node:20-alpine",
        source_filename="main.js",
        run_cmd=["node", "--max-old-space-size=200", f"{SOURCE_DIR}/main.js"],
        version_cmd=["node", "--version"],
        env={"NODE_OPTIONS": "--no-experimental-fetch", "NO_COLOR": "1"},
        comment_prefix="//",
    ),
    "cpp": LanguageProfile(
        id="cpp",
        display_name="C++17 (g++)",
        image="gcc:13",
        source_filename="main.cpp",
        compile_cmd=[
            "g++",
            "-std=c++17",
            "-O2",
            "-pipe",
            "-static",
            "-w",
            f"{SOURCE_DIR}/main.cpp",
            "-o",
            f"{WORKDIR}/program",
        ],
        run_cmd=[f"{WORKDIR}/program"],
        needs_exec_mount=True,
        version_cmd=["g++", "--version"],
        comment_prefix="//",
    ),
    "java": LanguageProfile(
        id="java",
        display_name="Java 21",
        image="eclipse-temurin:21-jdk-alpine",
        source_filename="Main.java",
        compile_cmd=["javac", "-d", WORKDIR, f"{SOURCE_DIR}/Main.java"],
        # Xshare:auto + TieredStopAtLevel=1 cut JVM startup, which otherwise
        # dominates the time budget on small test cases.
        run_cmd=[
            "java",
            "-XX:TieredStopAtLevel=1",
            "-XX:+UseSerialGC",
            "-Xss64m",
            "-cp",
            WORKDIR,
            "Main",
        ],
        needs_exec_mount=True,
        version_cmd=["java", "-version"],
        comment_prefix="//",
    ),
    "go": LanguageProfile(
        id="go",
        display_name="Go 1.22",
        image="golang:1.22-alpine",
        source_filename="main.go",
        compile_cmd=["go", "build", "-o", f"{WORKDIR}/program", f"{SOURCE_DIR}/main.go"],
        run_cmd=[f"{WORKDIR}/program"],
        needs_exec_mount=True,
        version_cmd=["go", "version"],
        env={"GOCACHE": f"{WORKDIR}/.gocache", "GOFLAGS": "-mod=mod", "HOME": WORKDIR},
        comment_prefix="//",
    ),
}


def get_profile(language: str) -> LanguageProfile:
    profile = PROFILES.get(language.strip().lower())
    if profile is None:
        raise ValueError(
            f"Unsupported language {language!r}. Supported: {', '.join(sorted(PROFILES))}"
        )
    return profile


def supported_languages() -> list[str]:
    return sorted(PROFILES)


def required_images() -> list[str]:
    return sorted({p.image for p in PROFILES.values()})
