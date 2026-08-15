"""
Entry point.

    python run_demo.py        list the scenarios
    python run_demo.py 1      run one
    python run_demo.py all    run every scenario in order

Scenario 1 and 2 are the two the brief asks for. Scenarios 3
and 4 are additional edge cases: a total blackout, and the
market-closed case where an old price is the correct one.
"""

import sys

import scenarios


def usage():
    print("")
    print("  Price feed reconciliation agent")
    print("")
    print("  Usage: python run_demo.py <scenario>")
    print("")
    for key in sorted(scenarios.SCENARIOS):
        title = scenarios.SCENARIOS[key][0]
        print("    " + key + "    " + title)
    print("    all  run every scenario in order")
    print("")


def main():
    if len(sys.argv) < 2:
        usage()
        return

    choice = sys.argv[1].lower()

    if choice == "all":
        for key in sorted(scenarios.SCENARIOS):
            scenarios.SCENARIOS[key][1]()
        return

    entry = scenarios.SCENARIOS.get(choice)
    if entry is None:
        print("")
        print("  No scenario called " + repr(choice))
        usage()
        return

    entry[1]()


if __name__ == "__main__":
    main()