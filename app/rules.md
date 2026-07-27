# COMPANY RULEBOOK — Solitude Flame / The Sol Factory
# ---------------------------------------------------
# This file is business logic, not code. It is appended to the AI's
# instructions on every extraction call, whichever model is used (Gemini,
# Claude, or any future one). Edit it in plain language and redeploy —
# the agent's judgment changes without touching code.

## WHO IS WHO

Our own people (their mails NEVER create tasks; their replies move task status):
- Braj Bhushan (owner/director), Krishan Mohan (logistics@), Lokesh Kumar,
  Yogender Chaudhary and Dev Mathuriya (production/stickers), Sunil,
  sales@thesolfactory.com, ai@thesolfactory.com — anyone @thesolfactory.com.

Clients (buy our cones; their asks are usually orders, samples, prices, specs):
- Roboroots (Steven Watts, Karen Luis), Cannara Biotech (Tommy Labrecque,
  Rafael Pungue), WeedMe, Organigram Global, Jointcraft (Elliot Gilbert,
  Daniel Milani) — and any new company asking about cones.

Freight forwarders / logistics partners (their asks are shipment work):
- UPS SCS (Sukhdheer Rana, Ankit Mewati), Kuehne+Nagel (Santosh Singh,
  Rakesh Ranjan), Logitrust / LTX (Amit, Divesh Pokhriyal, Dharmendra Singh),
  DHL, Committed Cargo (goezigo).

Banks (compliance/document requests only): ICICI, IndusInd.

## THE THREE SHIPMENT CHANNELS — when is a shipment task DONE?

Every shipment falls in exactly one channel. If the channel is unclear,
assume Channel A (the strictest) and keep the task open.

CHANNEL A — SOL-managed shipment (we are responsible door-to-door).
  Signals: DDP terms quoted or mentioned; SOL books the forwarder (LTX/DHL
  DDP quotes, courier/sample shipments we dispatch); we pay freight.
  Status logic:
  - Booking made, checklist shared, docs sent, vehicle arranged, dispatched,
    tracking/AWB shared --> all of these are ONLY in_progress. NOT done.
  - DONE only on PROOF OF DELIVERY: "delivered to consignee", "all box
    delivered", POD received/attached, client confirms receipt.

CHANNEL B — Client-arranged shipment (client is responsible for freight).
  Signals: EX-WORKS or FOB in checklists/Incoterms; forwarder writes "we
  have been advised by our overseas customer"; client says "my forwarder /
  RR will organize the freight"; client's nominated forwarder (e.g. K+N
  acting for the overseas buyer) contacts us.
  Status logic:
  - Our job ends at HANDOVER. "Handed over to <forwarder>", "pickup
    completed", forwarder confirms cargo received + export docs shared
    --> the shipment task is DONE.
  - Everything before handover (readiness dates, checklist, s.bill, e-way
    bill, vehicle/LR copy, pickup scheduling) --> in_progress.

CHANNEL C — POD-only clients.
  Some clients require proof of delivery regardless of who ships.
  Currently: (none named — add client names here as agreed).
  Status logic: identical to Channel A — nothing is done until POD.

## STATUS TRANSITIONS — what our replies mean

Our team's own words map to status like this (learned from real mail):
- "Received, thank you" / "Will update tomorrow" / "We will share the
  ship-ready date" / "Noted" --> in_progress. Never done.
- Sharing a CRD or readiness date ("CRD is 27th July") --> in_progress.
- "PFA ..." (checklist, Excel, invoice, AWB, packing list) --> if the task
  WAS "send document X", that task is done; the umbrella shipment task
  stays per its channel.
- "Checklist is approved" / "Approved." --> the approval task is done.
- "Handed over to <forwarder>" --> done for Channel B; in_progress for
  Channel A/C (still awaiting delivery).
- "Dispatched" / tracking number shared --> done ONLY for small courier
  sample shipments in Channel B-like situations; for Channel A/C stay
  in_progress until POD.
- Reminders ("Kind Reminder! Waiting for pickup") --> in_progress, and this
  signals the task may deserve high priority.
- A client saying just "thanks" / "great" is NOT proof of delivery.

## SMALL TASKS vs UMBRELLA SHIPMENT TASKS

A shipment generates many small tasks (approve checklist, share vehicle
details, provide s.bill, send invoice). Each small task closes when its
specific ask is answered. The umbrella task ("ship PO26107 to Roboroots")
closes ONLY per its channel rule above. Do not close the umbrella task
because a small task inside it finished.

## PRIORITY RULES

- A forwarder blocked waiting on us (vehicle at premises, checklist pending
  approval, pickup waiting) --> high priority: delay costs money daily.
- CRD or deadline within 48h --> high priority.
- Bank compliance requests (ICICI/IndusInd documents) --> high priority,
  deadline matters even when not stated.
- New sample requests / price questions --> normal, unless client states
  urgency.

## GENERAL

- PO numbers look like PO26xxx; Cannara POs look like QCPOxxxxxx. Mention
  the PO number in the task request text whenever it appears in the mail.
- The same PO discussed by client AND forwarder = one shipment; link the
  work in your head, don't duplicate tasks.
- When genuinely unsure between in_progress and done: choose in_progress.
  A task wrongly left open costs one click; a task wrongly closed loses
  a shipment.
