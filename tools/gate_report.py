"""List gate checks, label them, and compare decision rules on labelled files.

    python tools/gate_report.py list
    python tools/gate_report.py label <id> genuine|fake
    python tools/gate_report.py report
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bandarr.state import State  # noqa: E402

st = State(ROOT / "data" / "gate_lab" / "lab.db")
cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
if cmd == "list":
    for r in st.db.execute("""SELECT g.id, g.lv_verdict, g.lv_cutoff_hz, g.fc_verdict, g.fc_confidence, g.label,
                                     t.artist, t.title FROM gate_checks g JOIN tracks t USING(wishlist_key) ORDER BY g.id"""):
        print(f"{r['id']:>3} LV={r['lv_verdict']:8} {r['lv_cutoff_hz']/1000:3.0f}k  FC={r['fc_verdict'] or '-':12} {r['fc_confidence'] or 0:.2f}"
              f"  label={r['label'] or '-':8} {r['artist'][:22]} - {r['title'][:30]}")
elif cmd == "label":
    st.label_gate(int(sys.argv[2]), sys.argv[3])
    print("ok")
elif cmd == "report":
    for rule, c in st.gate_report().items():
        print(f"{rule:9} fake caught {c['fake_caught']}, fake missed {c['fake_missed']}, "
              f"genuine refused {c['genuine_refused']}, genuine passed {c['genuine_passed']}")
