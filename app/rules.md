# COMPANY RULEBOOK — Solitude Flame / The Sol Factory
# ---------------------------------------------------
# This file is business logic, not code. It is appended to the AI's
# instructions on every extraction call, whichever model is used (Gemini,
# Claude, or any future one). Edit it in plain language and redeploy —
# the agent's judgment changes without touching code.

## WHO IS WHO

Our own people (their mails NEVER create tasks; their replies move task
status) — anyone @thesolfactory.com. The company has NINE departments, and
EVERY task must be assigned to exactly one via the "department" field:

1. "admin" — Braj Bhushan (braj@) and Lokesh Kumar (info@). Management,
   sales & client relations: orders, samples, client pricing, product specs,
   ship-ready commitments, supplier price lists, and ANY task that does not
   clearly belong to another department.
2. "logistics" — Krishan Mohan (logistics@) and Devendar. Forwarders and
   shipping: booking, checklists, Incoterms, shipping docs (invoice/AWB/
   packing list/LUT/LR/s.bill/e-way bill), vehicle details, pickups,
   tracking, delivery/POD follow-up.
3. "production" — Yogender Chaudhary (production@). Manufacturing: cone
   sizes, quantities, sticker counts, production schedules, quality
   approvals on manufactured goods.
4. "accounts" — Harsh (account@, "Accounts Sol France"). Banking (ICICI,
   IndusInd), compliance documents, payments in/out, deposits, invoicing
   money matters.
5. "design" — Amit (our in-house designer — NOT the same person as LTX
   Amit at Logitrust, who is an external forwarder). Stickers, filter
   templates, dielines, artwork approvals, packaging design.
   Dev Mathuriya (sample@) also works here on samples.
6. "implementation" — order execution tracking. Checks the production
   status of every PO and follows the logistics work through: "check
   production progress of PO26107", "confirm the pickup actually happened",
   "is the order on schedule for its CRD?", chasing pending POs across
   departments. Assign here when the ask is to FOLLOW UP or VERIFY progress
   of an order, rather than to do the production/shipping work itself.
7. "qc" — quality control. COAs and quality certificates, client quality
   complaints or defect reports, inspection requests, pre-shipment quality
   checks, product spec verification ("are these bleached rice paper?").
8. "management" — top-level oversight (sees every department's dashboard).
   Assign here ONLY for escalations, disputes, company-level decisions, and
   negotiations that no single department can resolve. Routine work always
   goes to the specific department.
9. "hr" — human resources: hiring and recruitment, employee matters, leave
   and attendance, payroll inputs, staff welfare, internal HR communications.

Also: ai@ (SOL AI hub inbox, Priyanka Bharwani priyanka@) — internal
coordination, not a department.

Department assignment examples:
- "Share LR copy with UPS" -> logistics
- "Approve 109/26mm filter template" -> design
- "Submit compliance docs to ICICI" -> accounts
- "Change sticker quantity 40 to 48" -> production
- "Send sample packs / share price quote to client" -> admin
- "Client asks: any update on our order?" -> implementation
- "Check whether PO26105 production is on schedule" -> implementation
- "Provide COAs for gummed paper" -> qc
- "Client complains cones arrived damaged/wrong spec" -> qc
- "Forwarder dispute over 22% duty charged instead of 10%" -> management
- Unsure -> admin (Braj & Lokesh see everything and can reassign).

When writing a task's "request", word it so the right department instantly
recognises their work.

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

## WHEN IS A MESSAGE "ALREADY COVERED"? (STRICT)

Skipping a message because "a task already covers it" is allowed ONLY when
the open task list contains a task from the same sender/company for the
same specific request, and the new message adds nothing new at all. If in
any doubt — different wording, extra detail, new date/quantity/price, a
reminder, a chase, a different person asking — CREATE the task. It is far
cheaper for admin to merge two duplicate tasks than to lose one real ask.

Examples:
- Client emails "any update on our samples?" while a samples task is open
  -> NOT covered: create a follow-up/chase task (implementation).
- Forwarder resends the identical checklist mail twice in one hour, task
  already open for it -> covered, may skip citing that task id.
- Client repeats a quote request but changes quantity 500k -> 750k
  -> NOT covered: new task, resolve the old one.

## GENERAL

- PO numbers look like PO26xxx; Cannara POs look like QCPOxxxxxx. ALWAYS
  fill the task's "po_number" field when a PO/order number appears in or is
  clearly implied by the mail — this links the task to the order's record.
  Also mention the PO number in the task request text.
- The same PO discussed by client AND forwarder = one shipment; link the
  work in your head, don't duplicate tasks.
- When genuinely unsure between in_progress and done: choose in_progress.
  A task wrongly left open costs one click; a task wrongly closed loses
  a shipment.
