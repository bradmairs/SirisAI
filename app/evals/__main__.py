"""python -m app.evals [--selection-only] -- run the tool-choice evals
against this machine's configuration (.env) and print a report."""

import json
import sys

from app.db.base import Base
from app.db.session import engine
from app.evals import tool_choice


def main() -> None:
    import app.db.models  # noqa: F401  (register tables)
    from app.core.siris import get_siris

    Base.metadata.create_all(bind=engine)
    siris = get_siris()
    report = {"selection": tool_choice.selection_report(siris)}
    if "--selection-only" not in sys.argv:
        report["choice"] = tool_choice.choice_report(siris)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
