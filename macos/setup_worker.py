"""Fixed, unprivileged first-run entry bundled with the native launcher."""
import argparse
from pathlib import Path
from bridge_setup.core import Setup, default_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installer', type=Path, required=True)
    args = parser.parse_args()
    Setup(default_state(), Path(__file__).resolve().parent / 'payloads').run(args.installer)


if __name__ == '__main__':
    main()
