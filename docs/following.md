# Following, and the interval before an Accept

What FIRM does when a client follows someone, why it does it that way, and where the
ActivityPub model leaves a gap. The behaviour described here is the one in
`firm/core/services/activitypub/outbox.py` and `…/inbox.py`; the gap is where FEP-4ccd
would go if it is ever adopted.

## The gap

ActivityStreams gives an actor `followers` and `following`, and nothing between them. A C2S
client sends `Follow` to its server, the server delivers it, the followed actor's server
answers `Accept` or `Reject`, and only then does the relationship appear in any document.

Everything between "the client asked" and "the answer came back" is invisible. On a
fast LAN that is a few seconds; against a slow or unreachable peer it is forever, and the
server has no record at all that the client ever asked. The practical effect: a client
cannot distinguish *requested* from *not following*, so it either lies (shows the button
as if nothing happened) or polls.

## What FIRM does

`Follow` from the C2S outbox now adds the followed actor to `following` immediately
(`outbox.py::_process_outbox_follow`, dispatched from `process_outbox`). The collection is
a set, so a repeat `Follow` changes nothing, and two later activities undo it:

| Activity | Where | Effect on `following` |
| --- | --- | --- |
| `Reject{Follow}` | `inbox.py::_process_inbox_reject` | actor removed |
| `Undo{Follow}` | `outbox.py::_process_outbox_undo_follow` | actor removed |
| `Accept{Follow}` | `inbox.py::_process_inbox_accept` | actor added if absent (no duplicate) |

This is what Mastodon does: an optimistic row on the sender's side, removed when the
follow is refused or undone. No activity on the wire changes — `following` is a local
view, and peers only ever see the `Follow`/`Accept`/`Reject` activities themselves.

Consequences worth stating plainly:

- A follow that is never answered stays in `following` indefinitely. That matches
  Mastodon, and it is the honest reading of "we sent a Follow and have not been told
  otherwise" — but it does mean `following` is not proof that the other side agreed.
- Nothing tells the client *when* the answer arrives. It must reload the collection; the
  reload now shows the actor from the start, so the earlier "click Follow, see nothing"
  window is closed.
- `following` is read-authorized, so the optimistic entry is not visible to anyone but
  the actor and its server.

Tests: `tests/core/test_outbox_follow.py` (add on Follow, set semantics, `Undo`, `Reject`,
missing object).

## The shared inbox, and the unaddressed Follow

`shared_inbox_path` is `"shared"` by default: each tenant advertises
`endpoints.sharedInbox = <prefix>/shared` and a signed POST to that path is routed to the
local actors the activity names in `to`/`cc`. Setting the key to `null` (or `""`) removes
the endpoint and the route — supported since 2026-11-07 — but the testbed runs with it on.

The one scenario that fails with it on is an inbound follow from Pixelfed. Its `Follow` is
built by `app/Transformer/ActivityPub/Verb/Follow.php` and carries only `@context`, `type`,
`actor` and `object` — no `to`, no `cc` — and the delivery job posts to
`$target->sharedInbox ?? $target->inbox_url`
(`app/Jobs/FollowPipeline/FollowActivityPubDeliver.php:54`). A shared inbox is a route, not a
box, so FIRM must decide which local actor an activity is for from its addressing alone. An
unaddressed Follow names nobody: the route accepts it with 202, hands it to no actor, and
stores nothing. The sender reads 202 as success, so the follow silently dies — no bounce, no
retry, no Accept.

Delivered instead to `/actors/<name>/inbox`, the owner is known from the path and the follow
completes; that is the "implicit `to` for a personal inbox" case, and why disabling the shared
inbox was enough to unblock the test.

For this case the shared inbox now reads the subject. `_follow_subject_uri()` takes the
actor from a `Follow`'s `object` - and, one level down, from the `Follow` an `Undo` carries -
when the addressing names no local actor, and `post_to_shared_inbox` routes to that actor
through the same `post_to_inbox` call every other delivery uses. Nothing about inbox
processing is duplicated: the shared inbox still only decides *who*, and the personal inbox
path does the rest. Verified end to end: an unaddressed Follow posted to `/shared` lands in
the object actor's inbox, adds the sender to its `followers`, and answers 202 while the
Accept goes out.

Two boundaries are deliberate:

- **Addressed collections are still not expanded.** A sender that puts `…/alice/followers`
  in `to`/`cc` is making a claim about alice's audience, not a grant to write into every
  member's inbox; expanding it would let one signed POST fan out to an unbounded set of
  local actors. If that case is ever needed, the collection maps to its **owner** - one
  recipient, who then decides - not to its members.
- **A subject that is not a local actor is not a recipient**, so following someone on
  another server is not something this route can be used to deliver.

What the shared inbox still cannot do on its own is carry pushed content: a remote `Create`
addressed `to: [#Public], cc: [the sender's own followers]` names no local actor, and
unlike a `Follow` it has no single subject. That case is Rule 3, below.

## Rule 3: routing by relationship

A peer posts to the author's `followers` collection and never enumerates it, so a receiving
server cannot learn the collection's items — it can only ask **who here follows the author**.
`mastodon_deliver_shared_inbox_activity()`, called from `post_to_shared_inbox`, does that.

- **Only a followers URI triggers it.** The URI comes from the author document's `followers`
  property — never assembled from the actor's id — and is compared literally with the
  activity's `to`/`cc` and with those of its embedded object. A post naming some other
  collection reaches nobody through this path.
- **`#Public` is not a recipient.** It is a visibility designation: it governs who may *read*
  the stored document, not who is handed a copy. A post addressed to `#Public` alone is not
  fanned out by this rule.
- **Specific recipients stay Rule 1's business.** A local actor named in `to`/`cc` is
  delivered to directly by the named-owner loop and is passed to this pass as `exclude`, so
  it cannot receive a second copy.
- **The recipient set comes from our own store.** `_local_followers()` walks the tenant's
  actor documents and keeps the local actors whose `following` collection holds the author:
  one `contains_item` per local actor, no request to the remote collection, no page or count
  of it. Delivery is in process through `deliver_to(..., local_only=True)`, so each
  recipient's inbox is the record and no peer is posted to.
- **The signer must be the author**, as for any other inbox POST: an activity claiming
  someone else's authorship reaches none of that actor's followers.

A local follow enters `following` when the client posts `Follow`, before any `Accept`
arrives (see above), so an unanswered follow is already a recipient. That matches
Mastodon's optimistic row; FEP-4ccd's `pendingFollowing` is the shape that would change it.

If Pixelfed added `to: [target]` to its `Create` transformer (one line, in the testbed's
`patches/` overlay), Rule 1 would carry this traffic and no receiver would need to infer the
audience at all. Rule 3 exists because it does not.

## FEP-4ccd, and what adopting it would change

[FEP-4ccd](https://codeberg.org/fediverse/fep/src/branch/main/fep/4ccd/fep-4ccd.md)
(*Pending Followers Collection and Pending Following Collection*, Evan Prodromou, DRAFT,
2023-06-21) gives the interval a name: two actor properties, `pendingFollowers` and
`pendingFollowing`, each a collection of `Follow` **activities** — not actors — newest
first, unique by `id`, and unique per actor (`pendingFollowers`) or per `object`
(`pendingFollowing`). A `Follow` joins the collection when the server accepts it, and
leaves on `Accept`, `Reject` or `Undo`.

For FIRM and its clients that would mean:

- `_process_outbox_follow` writes the `Follow` activity into a new
  `<actor>/pendingFollowing` collection instead of the actor URI into `following`;
- `Accept` moves the entry from `pendingFollowing` to `following`; `Reject` and `Undo`
  delete it;
- the actor document advertises `pendingFollowing` beside `following`, served with the
  same read authorization;
- the client shows "requested" from `pendingFollowing`, and reloads `following` when the
  entry disappears — no polling of `following`, and the notifications feed gets a real
  event to render.

Why it is not implemented now: it is a draft with no mainstream implementations, it adds
a collection plus an authorization surface plus a client path, and the observable
behaviour for our clients is the same as the optimistic row — the actor appears at once,
and disappears if the follow is refused. The difference is bookkeeping: FEP-4ccd keeps
`following` truthful and puts the uncertainty in a second collection, at the cost of
every client having to know about that collection.

If the choice is revisited, the change is confined to `_process_outbox_follow`,
`_process_inbox_accept`, `_process_inbox_reject`, `_process_outbox_undo_follow`, the actor
document builder, and the client's following view. `tests/core/test_outbox_follow.py`
names the behaviours that must stay true either way: add on Follow, no duplicates, gone
after `Undo`, gone after `Reject`.
