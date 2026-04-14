"""
jupytur-install — register the Jupytur kernelspec with Jupyter.

Usage:
    jupytur-install           # installs for the current user
    jupytur-install --system  # installs system-wide (requires root/sudo)
"""

import argparse
import os
import shutil
import sys
import tempfile

from jupyter_client.kernelspec import KernelSpecManager


def main():
    parser = argparse.ArgumentParser(description="Install the Jupytur kernel")
    parser.add_argument(
        "--system",
        action="store_true",
        help="Install into the system kernel directory instead of user directory",
    )
    args = parser.parse_args()

    src = os.path.join(os.path.dirname(__file__), "kernelspec")

    with tempfile.TemporaryDirectory() as td:
        # KernelSpecManager.install_kernel_spec expects the directory to be
        # named after the kernel; copy kernelspec/ → <tmp>/jupytur/
        dest = os.path.join(td, "jupytur")
        shutil.copytree(src, dest)

        ksm = KernelSpecManager()
        path = ksm.install_kernel_spec(
            dest,
            kernel_name="jupytur",
            user=not args.system,
            replace=True,
        )

    print(f"Installed Jupytur kernelspec to {path}")
    print()
    print("Before starting Jupyter, set:")
    print("  export JUPYTUR_URL=http://localhost:8080")
    print("  export JUPYTUR_CODE=$(cat ~/.urbit/zod/.http.ports | ...)  # or paste +code output")
    print("  export JUPYTUR_SHIP=zod")


if __name__ == "__main__":
    main()
