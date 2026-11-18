"""Short-lived Linux job for direct Access/Jet operations; simulation by default."""
import argparse
import json
import sys

from .access_jet import (
    AccessJetError, compare_and_set_numeric_property, inspect_numbering, load_config,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    update = commands.add_parser("numeric-property")
    update.add_argument("--name", required=True)
    update.add_argument("--expected", required=True)
    update.add_argument("--replacement", required=True)
    update.add_argument("--apply", action="store_true")
    options = parser.parse_args(argv)
    try:
        config = load_config(options.config_file)
        result = (inspect_numbering(config) if options.command == "inspect" else
                  compare_and_set_numeric_property(config, options.name,
                                                   options.expected, options.replacement,
                                                   apply=options.apply))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except AccessJetError as exc:
        print(json.dumps({"status": "error", "code": exc.code,
                          "outcome_unknown": exc.outcome_unknown,
                          "automatic_retry_allowed": False}))
        return 1
    except Exception:
        # Never emit the driver traceback or a connection string to job logs.
        print(json.dumps({"status": "error", "code": "ACCESS_JOB_FAILED",
                          "automatic_retry_allowed": False}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
