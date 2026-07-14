"""Offline sanity tests: fixtures mirror the anchor/text order of each
real site (July 2026). Run: python scraper/test_parsers.py"""
from bs4 import BeautifulSoup
import scrape as S

AG = """<html><body>
<span>Live Event</span><span>| Dance, Featured, Theatre</span>
<a href="https://angrianan.com/event/iadf-2026-beneath-earth-and-sky/"><img src="x.jpg"></a>
<a href="https://angrianan.com/event/iadf-2026-beneath-earth-and-sky/">More Info</a>
<a href="https://angrianan.ticketsolve.com/ticketbooth/shows/873662785/events/128646839/seats?zone=Default">BOOK NOW</a>
<p>Friday July 17</p>
<h3><a href="https://angrianan.com/event/iadf-2026-beneath-earth-and-sky/">IADF 2026: Between Earth and Sky</a></h3>
<span>Live Event</span><span>| Workshop</span>
<a href="https://angrianan.com/event/summer-camp/"><img src="y.jpg"></a>
<a href="https://angrianan.com/event/summer-camp/">More Info</a>
<a href="https://angrianan.ticketsolve.com/ticketbooth/shows/873664491">BOOK NOW</a>
<p>Monday July 13</p><p>- Friday July 17</p>
<h3><a href="https://angrianan.com/event/summer-camp/">Summer Camp for ages 13 to 18</a></h3>
<span>Live Event</span><span>| Comedy, Featured</span>
<a href="https://angrianan.ticketsolve.com/ticketbooth/shows/873662942">BOOK NOW</a>
<p>Friday January 8</p>
<h3><a href="https://angrianan.com/event/deirdre-okane/">Deirdre O'Kane: All The Rage</a></h3>
</body></html>"""

RCC = """<html><body>
<a href="https://regionalculturalcentre.com/whats-on/">What's On</a>
<span>Earagail Arts Festival, Music</span><span>Live Event</span>
<a href="https://regionalculturalcentre.com/events/gwenno-concert/">Gwenno (Concert)</a>
<p>Tue Jul 14, 8:00 pm</p>
<a href="https://regionalculturalcentre.com/events/gwenno-concert/"></a>
<span>Exhibition</span>
<a href="https://regionalculturalcentre.com/exhibitions/iron-gates/">Barbara Knezevic: The Iron Gates (Gallery 1)</a>
<p>Sat Jul 4, 6:00 pm</p><p>Sat Aug 29, 5:00 pm</p>
<span>Music</span><span>Live Event</span>
<a href="https://regionalculturalcentre.com/events/lemoncello/">Lemoncello (Concert)</a>
<p>Fri Nov 13, 8:00 pm</p>
<a href="https://regionalculturalcentre.com/past-events-concerts/">Past Events</a>
</body></html>"""

BALOR = """<html><body>
<a href="https://www.balorartscentre.com/?page_id=87">EVENTS</a>
<img src="a.jpg"><a href="https://www.balorartscentre.com/?event=the-finn-valley-mens-choir">The Finn Valley Men's Choir</a>
<p>10 Jul 26</p>
<img src="b.jpg"><a href="https://www.balorartscentre.com/?event=we-will-rock-you">We Will Rock You</a>
<p>31 Jul 26</p>
<a href="https://balorartscentre.ticketsolve.com/#/shows">Book Online Now</a>
</body></html>"""

ABBEY = """<html><body>
<a href="https://abbeycentre.ticketsolve.com/shows">WHAT'S ON</a>
<h1>THE SEEGER SESSIONS REVIVAL</h1>
<a href="https://abbeycentre.ticketsolve.com/ticketbooth/shows">BOOK NOW</a>
<a href="https://abbeycentre.ticketsolve.com/ticketbooth/shows/1173672330"><img src="s.jpg"></a>
<b>30</b>Jul, 2026 <span>Upcoming</span>
<a href="https://abbeycentre.ticketsolve.com/ticketbooth/shows/1173672330">The Seeger Sessions Revival</a>
<span>Abbey Arts Centre</span>
<a href="https://www.facebook.com/sharer/sharer.php?u=https://abbeycentre.ie/eventer/the-seeger-sessions-revival/edate/2026-07-30">fb</a>
<a href="https://twitter.com/intent/tweet?source=https://abbeycentre.ie/eventer/the-seeger-sessions-revival/edate/2026-07-30">tw</a>
<a href="https://abbeycentre.ticketsolve.com/ticketbooth/shows/1173674806"><img src="m.jpg"></a>
<a href="https://abbeycentre.ticketsolve.com/ticketbooth/shows/1173674806">Mikaela - A Night of Adele</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=https://abbeycentre.ie/eventer/mikaela-a-night-of-adele/edate/2026-08-29">fb</a>
</body></html>"""


def show(name, events):
    print(f"--- {name}: {len(events)} events")
    for e in events:
        print("   ", e["date"], e.get("end_date", ""), "|", e["title"],
              "|", e.get("category", "-"), "|", e.get("time", ""),
              "|", e.get("url", "")[:60])
    return events


src = dict(name="t", venue="V", town="T", county="Donegal")
ag = show("An Grianan", S.parse_an_grianan(BeautifulSoup(AG, "lxml"), src))
rcc = show("RCC", S.parse_rcc(BeautifulSoup(RCC, "lxml"), src))
bal = show("Balor", S.parse_balor(BeautifulSoup(BALOR, "lxml"), src))
abb = show("Abbey", S.parse_abbey(BeautifulSoup(ABBEY, "lxml"), src))

assert len(ag) == 3 and ag[0]["title"].startswith("IADF")
assert ag[1].get("end_date") == "2026-07-17" and ag[1]["date"] == "2026-07-13"
assert ag[2]["date"] == "2027-01-08", ag[2]["date"]  # year rollover
assert ag[0]["category"] == "Dance, Theatre"
assert ag[0]["booking_url"] and "873662785" in ag[0]["booking_url"]
assert len(rcc) == 3
assert rcc[0]["time"] == "8:00 pm" and rcc[0]["category"] == "Earagail Arts Festival, Music"
assert rcc[1]["end_date"] == "2026-08-29"
assert len(bal) == 2 and bal[0]["date"] == "2026-07-10"
assert len(abb) == 2 and abb[0]["date"] == "2026-07-30"
assert abb[0]["url"].startswith("https://abbeycentre.ie/eventer/")
print("\nAll assertions passed.")
