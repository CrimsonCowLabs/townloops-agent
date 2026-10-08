# Ticket / price info — what counts

TownLoops has no ticket fields (decided; don't propose them). The detail-page
pass stores the page's own price wording in `events.price` and the ticket/RSVP
link in `events.rsvp_url`; event-ship turns them into the `free` tag or one
ticket line atop the description (`engine/event_tickets.py`).

A wrong price or a wrong `free` is worse than none. Null beats wrong.

## Price

- Kept only if the quote is on the page **and**, read to the end of its
  sentence there, still states a cost. A fragment like "Free" from "Free
  parking in lot" is dropped.
- **Free** (tag `free`, no ticket line): "free admission", "free and open to
  the public", "$0", "no charge". Not free: "free with museum admission",
  free parking/snacks/drinks, gluten-free anything.
- **Paid** (`Tickets: $25–$40`): any non-zero amount to get in, even beside a
  free tier ("Free for kids, $15 adults"). Amounts in a clause about parking,
  drinks, food or merch don't count ("Free admission; parking $5" is free).
- **Sold out / waitlist** with no price and no "free" → `Tickets: sold out`.
  "Not sold out yet" is not sold out.
- **Donation** — "by donation", "pay what you can", "$10 suggested",
  "suggested donation" → `Tickets: by donation, $10 suggested`. Never free.
  "Ages 12+ suggested" is not a donation.

## Link

An http(s) URL that is on the page and is a ticketing/registration page or
the event's own page. Never a homepage (`/`, `/home`, `/index.html`) or the
seed's listing page. A link with no stated cost (or on a free event) ships as
`RSVP: <link>`.

## Shipping

The line is built at ship time, so the local description and content hash
never change. On a live row it replaces an earlier line of exactly our format,
never a source's own sentence ("Tickets: $20 at the door."). A priced event
drops `free` from the live tags.
