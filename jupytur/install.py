"""
jupytur-install — register a Jupytur kernel variant with Jupyter.

Variants are keyed by the Gall desk/agent name they default to (e.g.
'north', 'plan').  The exception is 'hoon', which is the legacy display
name for the kernel that defaults to %dojo.  Install variants separately:

    jupytur-install                   # default: hoon variant -> %dojo
    jupytur-install --variant north   # north variant  -> %north
    jupytur-install --all             # install every known variant

The --system flag installs into the system-wide directory instead of
the user directory.
"""

import argparse
import json
import os
import sys
import tempfile

from jupyter_client.kernelspec import KernelSpecManager


# Adding a new variant is one entry: kernel name (the directory installed
# under .../kernels/), display label, default agent, and language_info for
# Jupyter's editor.  At runtime the kernel reads JUPYTUR_AGENT from env.
VARIANTS = {
    # 'hoon' is the legacy variant name for the dojo-backed kernel — the
    # display reads "Jupytur (Hoon)" rather than "Jupytur (Dojo)" because
    # Hoon is the language users associate with that kernel.  All other
    # variants are keyed by the desk/agent name they default to.
    "hoon": {
        "kernel_name": "jupytur",
        "display_name": "Jupytur (Hoon)",
        "language": "hoon",
        "default_agent": "dojo",
        "language_info": {
            "name": "hoon",
            "mimetype": "text/x-hoon",
            "file_extension": ".hoon",
            "codemirror_mode": "hoon",
        },
        "help_links": [
            {"text": "Hoon School", "url": "https://developers.urbit.org/courses/hoon-school"},
            {"text": "Hoon Reference", "url": "https://developers.urbit.org/reference/hoon/overview"},
        ],
    },
    "north": {
        "kernel_name": "jupytur-north",
        "display_name": "Jupytur (North)",
        "language": "forth",
        "default_agent": "north",
        # North maintains the Forth interpreter state (including the data
        # stack) globally on the agent — it isn't keyed by sole session.
        # Define and invoke a stack-clear word at every fresh connect so
        # notebook authors start with an empty stack.
        "init_command": ": SCLR BEGIN DEPTH WHILE DROP REPEAT ; SCLR",
        "language_info": {
            "name": "forth",
            "mimetype": "text/x-forth",
            "file_extension": ".fs",
            "codemirror_mode": "forth",
        },
        "help_links": [
            {"text": "North on GitHub", "url": "https://github.com/sigilante/north"},
            {"text": "ANSI Forth (DPANS94)", "url": "https://github.com/sigilante/north/blob/master/docs/DPANS94.txt"},
        ],
    },
}


def _kernel_json(variant):
    env = {
        "JUPYTUR_AGENT": variant["default_agent"],
        "JUPYTUR_LANGUAGE": variant["language"],
    }
    if variant.get("init_command"):
        env["JUPYTUR_INIT_COMMAND"] = variant["init_command"]
    return {
        "argv": ["python", "-m", "jupytur.kernel", "-f", "{connection_file}"],
        "display_name": variant["display_name"],
        "language": variant["language"],
        "interrupt_mode": "signal",
        "env": env,
        "metadata": {
            "debugger": False,
            "description": (
                f"Jupytur kernel (variant={variant['language']}) — "
                f"REPL backed by a live Urbit ship via Eyre, default agent "
                f"%{variant['default_agent']}"
            ),
            "author": "N. E. Davis",
            "license": "MIT",
            "url": "https://github.com/sigilante/jupytur",
            "help_links": variant["help_links"],
        },
    }


def _install_one(variant_key, user):
    variant = VARIANTS[variant_key]
    with tempfile.TemporaryDirectory() as td:
        spec_dir = os.path.join(td, variant["kernel_name"])
        os.makedirs(spec_dir)
        with open(os.path.join(spec_dir, "kernel.json"), "w") as f:
            json.dump(_kernel_json(variant), f, indent=2)

        ksm = KernelSpecManager()
        path = ksm.install_kernel_spec(
            spec_dir,
            kernel_name=variant["kernel_name"],
            user=user,
            replace=True,
        )
    print(
        f"Installed {variant['display_name']} ({variant_key}) "
        f"-> {path} [default agent: %{variant['default_agent']}]"
    )


def main():
    parser = argparse.ArgumentParser(description="Install a Jupytur kernel variant")
    parser.add_argument(
        "--variant",
        choices=sorted(VARIANTS.keys()),
        default="hoon",
        help="Which language variant to install (default: hoon)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Install every known variant",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available variants and exit",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="Install into the system kernel directory instead of user directory",
    )
    args = parser.parse_args()

    if args.list:
        for key, v in VARIANTS.items():
            print(
                f"  {key:<8}  display={v['display_name']!r:<28} "
                f"kernel={v['kernel_name']!r:<20} agent=%{v['default_agent']}"
            )
        return

    targets = list(VARIANTS.keys()) if args.all else [args.variant]
    for key in targets:
        _install_one(key, user=not args.system)

    print()
    print("Configure the connection either via a %config cell or env vars:")
    print()
    print("  (1) %config cell at the top of your notebook:")
    print("        %config url=http://localhost:80 ship=zod code=<+code> agent=dojo")
    print()
    print("  (2) Environment before launching Jupyter:")
    print("        export JUPYTUR_URL=http://localhost:8080")
    print("        export JUPYTUR_CODE=<+code output>")
    print("        export JUPYTUR_SHIP=zod")


if __name__ == "__main__":
    main()
