"""Generate SAMPLE RSS feeds in tests/fixtures/ for tests and offline demos.

These stories are made up. They exercise clustering, balance and blindspot
detection without needing the internet.
Run:  python tests/make_fixtures.py
"""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent / "fixtures"
BASE = datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)

# (source_id, hours_ago, title, summary)
ITEMS = [
    # Story A: Fed holds rates (covered across the spectrum)
    ("npr", 2, "Federal Reserve holds interest rates steady, signals patience on cuts",
     "The Federal Reserve kept its benchmark interest rate unchanged on Wednesday, saying inflation remains above its 2% target while the labor market cools."),
    ("bbc-us", 3, "US Federal Reserve keeps interest rates on hold as inflation lingers",
     "The US central bank left interest rates unchanged, with chair Jerome Powell saying officials need more confidence that inflation is falling."),
    ("cnbc", 2, "Fed holds rates steady; Powell says cuts depend on inflation data",
     "Federal Reserve officials voted to hold interest rates steady. Markets had priced in a pause; stocks rose modestly after the decision."),
    ("foxbusiness", 1, "Fed keeps interest rates unchanged as Powell warns inflation fight not over",
     "The Federal Reserve held interest rates steady, a blow to borrowers hoping for relief on mortgages and credit cards as inflation persists."),
    ("washexaminer", 4, "Powell's Federal Reserve holds rates steady amid sticky inflation",
     "The Federal Reserve declined to cut interest rates, with critics saying government spending is keeping inflation elevated."),

    # Story B: Senate bill on AI chip exports
    ("guardian-us", 3, "Senate passes bipartisan bill tightening AI chip export controls",
     "The Senate voted 71-27 to tighten export controls on advanced AI chips, a measure backers say protects national security."),
    ("thehill", 2, "Senate approves AI chip export bill in bipartisan 71-27 vote",
     "Senators approved legislation restricting exports of advanced semiconductors used for artificial intelligence; the House has yet to act."),
    ("foxnews", 2, "Senate passes AI chip export crackdown aimed at China in 71-27 vote",
     "The Senate passed a bill cracking down on exports of AI chips to China. Tech industry groups warned the rules could hurt US companies."),
    ("nypost", 5, "Senate votes to curb AI chip exports to China, tech giants push back",
     "A bipartisan Senate majority voted to restrict AI chip exports, as tech lobbyists argued the bill goes too far."),

    # Story C: ceasefire talks
    ("aljazeera", 1, "Ceasefire talks resume in Cairo as mediators push for hostage deal",
     "Negotiators gathered in Cairo for a new round of ceasefire talks, with mediators pressing both sides on a phased hostage and prisoner exchange."),
    ("bbc-world", 2, "Ceasefire talks restart in Cairo with mediators seeking hostage deal",
     "Mediators from Egypt and Qatar hosted renewed ceasefire talks in Cairo aimed at a hostage release agreement."),
    ("foxnews", 3, "Hostage deal ceasefire talks resume in Cairo as pressure mounts",
     "Ceasefire talks resumed in Cairo. Families of hostages urged negotiators to reach a deal quickly."),
    ("dw", 4, "Cairo ceasefire talks resume, mediators eye hostage exchange",
     "A new round of ceasefire talks began in Cairo; diplomats described the mood as cautious."),

    # Story D: science
    ("nasa", 6, "NASA asteroid sample mission returns first images from Bennu flyby",
     "NASA released the first images from its asteroid mission's flyby, showing boulders on the surface of Bennu."),
    ("sciencedaily", 8, "Researchers analyze first images from NASA asteroid Bennu flyby",
     "Planetary scientists say the asteroid images reveal a rubble-pile surface on Bennu."),
    ("arstechnica", 5, "NASA's asteroid probe sends back striking Bennu flyby images",
     "The spacecraft's asteroid flyby produced the sharpest images yet of Bennu's boulder-strewn surface."),

    # Story E: covered only by left-leaning outlets (blindspot)
    ("guardian-us", 6, "Report finds climate adaptation funding cuts leave coastal towns exposed",
     "A new report says reductions in federal climate adaptation funding have left dozens of coastal towns without flood defenses."),
    ("vox", 7, "Climate adaptation funding cuts are leaving coastal towns exposed, report says",
     "The report on climate adaptation funding tallies stalled flood defense projects across coastal towns."),
    ("npr", 8, "Coastal towns exposed after climate adaptation funding cuts, report finds",
     "Local officials say the climate adaptation funding cuts have halted seawall and flood projects."),

    # Story F: covered only by right-leaning outlets (blindspot)
    ("foxnews", 4, "Governor signs school choice voucher expansion into law",
     "The governor signed a school choice voucher expansion that lets families use state funds for private school tuition."),
    ("dailycaller", 5, "School choice voucher expansion signed into law by governor",
     "Supporters of the school choice voucher expansion hailed the governor's signature as a win for parents."),
    ("washtimes", 6, "Governor signs sweeping school choice voucher expansion",
     "The school choice voucher expansion takes effect next fall, officials said."),

    # Investigation match + singles + muted
    ("dw", 3, "EU AI Act enforcement begins for general-purpose AI models",
     "European regulators began enforcing obligations under the AI Act for providers of general-purpose AI models."),
    ("reason", 6, "The case against occupational licensing for hair braiders",
     "A new study examines licensing rules for hair braiders across states."),
    ("newsweek", 3, "Daily horoscope for November 2: what the stars say",
     "Your horoscope for today."),
    ("pbs", 9, "Local orchestra celebrates 100th anniversary season",
     "A small-town orchestra marks its centennial with a season of free concerts."),
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    by_source: dict[str, list] = {}
    for sid, hours, title, summary in ITEMS:
        by_source.setdefault(sid, []).append((hours, title, summary))
    for sid, items in by_source.items():
        entries = []
        for n, (hours, title, summary) in enumerate(items):
            slug = "-".join(title.lower().split()[:6]).replace(",", "").replace("'", "")
            entries.append(
                f"<item><title>{escape(title)}</title>"
                f"<link>https://example.com/{sid}/{slug}?utm_source=rss</link>"
                f"<description>{escape(summary)}</description>"
                f"<pubDate>{format_datetime(BASE - timedelta(hours=hours))}</pubDate></item>")
        xml = ('<?xml version="1.0"?><rss version="2.0"><channel>'
               f"<title>{sid} (SAMPLE DATA)</title><link>https://example.com/{sid}</link>"
               + "".join(entries) + "</channel></rss>")
        (OUT / f"{sid}.xml").write_text(xml, encoding="utf-8")
    print(f"wrote {len(by_source)} sample feeds to {OUT}")


if __name__ == "__main__":
    main()
