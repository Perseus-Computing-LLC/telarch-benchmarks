-------------------- MODULE kibisis_lease_fencing --------------------
(***************************************************************************)
(* Formal TLA+ specification for Kibisis atomic concurrency leases         *)
(* with monotonic fencing tokens and fencing-aware storage sinks.          *)
(*                                                                         *)
(* Author: Perseus Computing LLC                                           *)
(* License: MIT License                                                    *)
(***************************************************************************)

EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Workers, MaxFencingToken, MaxTime

VARIABLES
    lease_holder,         \* Current active holder in coordinator (or "None")
    lease_expires_at,     \* Absolute expiration timestamp
    current_fencing_token,\* Monotonic counter in lease coordinator
    storage_highest_token,\* Highest fencing token committed in storage sink
    worker_held_token,    \* Mapping of worker -> fencing token from last acquisition
    clock                 \* Discrete wall-clock time

vars == <<lease_holder, lease_expires_at, current_fencing_token, storage_highest_token, worker_held_token, clock>>

TypeOK ==
    /\ lease_holder \in Workers \union {"None"}
    /\ lease_expires_at \in 0..MaxTime
    /\ current_fencing_token \in 0..MaxFencingToken
    /\ storage_highest_token \in 0..MaxFencingToken
    /\ worker_held_token \in [Workers -> 0..MaxFencingToken]
    /\ clock \in 0..MaxTime

Init ==
    /\ lease_holder = "None"
    /\ lease_expires_at = 0
    /\ current_fencing_token = 0
    /\ storage_highest_token = 0
    /\ worker_held_token = [w \in Workers |-> 0]
    /\ clock = 0

(***************************************************************************)
(* Actions                                                                 *)
(***************************************************************************)

Acquire(w, ttl) ==
    /\ (lease_holder = "None" \/ lease_expires_at < clock \/ lease_holder = w)
    /\ current_fencing_token < MaxFencingToken
    /\ clock + ttl <= MaxTime
    /\ lease_holder' = w
    /\ lease_expires_at' = clock + ttl
    /\ current_fencing_token' = current_fencing_token + 1
    /\ worker_held_token' = [worker_held_token EXCEPT ![w] = current_fencing_token + 1]
    /\ UNCHANGED <<storage_highest_token, clock>>

Release(w) ==
    /\ lease_holder = w
    /\ lease_expires_at >= clock
    /\ lease_holder' = "None"
    /\ lease_expires_at' = 0
    /\ UNCHANGED <<current_fencing_token, storage_highest_token, worker_held_token, clock>>

Tick ==
    /\ clock < MaxTime
    /\ clock' = clock + 1
    /\ UNCHANGED <<lease_holder, lease_expires_at, current_fencing_token, storage_highest_token, worker_held_token>>

WriteToSink(w) ==
    /\ worker_held_token[w] > 0
    /\ IF worker_held_token[w] > storage_highest_token
       THEN storage_highest_token' = worker_held_token[w]
       ELSE UNCHANGED storage_highest_token
    /\ UNCHANGED <<lease_holder, lease_expires_at, current_fencing_token, worker_held_token, clock>>

Next ==
    \/ \E w \in Workers, ttl \in 1..2: Acquire(w, ttl)
    \/ \E w \in Workers: Release(w)
    \/ \E w \in Workers: WriteToSink(w)
    \/ Tick

(***************************************************************************)
(* Safety Invariants                                                       *)
(***************************************************************************)

\* Invariant 1: Mutual Exclusion of Active Unexpired Leases
MutualExclusion ==
    (lease_holder /= "None" /\ lease_expires_at >= clock) =>
        (\A other \in Workers: (other /= lease_holder) => worker_held_token[other] < current_fencing_token)

\* Invariant 2: Monotonic Fencing Tokens
MonotonicFencing ==
    storage_highest_token <= current_fencing_token

\* Invariant 3: Fencing Sink Rejects Stale Writes
NoStaleOverwrites ==
    \A w \in Workers:
        (worker_held_token[w] < storage_highest_token) =>
            (storage_highest_token' /= worker_held_token[w])

Spec == Init /\ [][Next]_vars

=============================================================================
