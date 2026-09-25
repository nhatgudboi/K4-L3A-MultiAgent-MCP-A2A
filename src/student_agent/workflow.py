from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Evidence-first Multi-Agent collaborative investigation workflow for Day09 L3A.

    Architecture:
      1. Coordinator / Router: receives case, parses claim topics, dispatches specialists.
      2. Order/Item Agent: queries get_order and get_order_items to check status and entities.
      3. Shipment Agent: queries get_shipment_summary and get_sellers to check delivery timeline.
      4. Payment Agent: queries get_order_payments, get_payment_timeline, get_refund_timeline.
      5. Policy Agent: consults get_policy to arbitrate the ground truth issue, amounts, actions.
      6. Verifier Agent: validates schema, cross-field consistency, money totals, invariants.
    """
    case_id: str = case["case_id"]
    cust_req: dict[str, Any] = case.get("customer_request", {})
    claimed_order_id: str = cust_req.get("claimed_order_id", "")
    policy_version: str = case.get("policy_version", "EC_POLICY_V1")
    customer_claims: list[dict[str, Any]] = cust_req.get("claims", [])

    all_evidence_refs: list[str] = []
    domain_evidence: dict[str, list[str]] = {}

    def record_evidence(domain: str, ev_ref: str | None) -> None:
        if ev_ref and ev_ref not in all_evidence_refs:
            all_evidence_refs.append(ev_ref)
            domain_evidence.setdefault(domain, []).append(ev_ref)

    # -------------------------------------------------------------
    # 1. Coordinator: inspect claimed topics
    # -------------------------------------------------------------
    candidate_topics = [
        c.get("topic")
        for c in customer_claims
        if c.get("topic") and c.get("topic") != "requested_full_refund"
    ]
    customer_claimed_topic = candidate_topics[0] if candidate_topics else "unsupported_claim"

    # -------------------------------------------------------------
    # 2. Order/Item Agent: fetch order & item evidence
    # -------------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-item-agent",
        attributes={"task": "get_order", "order_id": claimed_order_id},
    )

    order_ev = await gateway.call("get_order", case_id=case_id, order_id=claimed_order_id)
    order_ref = order_ev.get("evidence_ref")
    record_evidence("order", order_ref)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-item-agent",
        tool_name="get_order",
        evidence_refs=[order_ref] if order_ref else [],
    )
    order_data: dict[str, Any] = order_ev.get("data", {})
    order_status: str = order_data.get("order_status", "")

    item_ids: list[str] = []
    seller_ids: list[str] = []
    items_ev = await gateway.call("get_order_items", case_id=case_id, order_id=claimed_order_id)
    items_ref = items_ev.get("evidence_ref")
    record_evidence("item", items_ref)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-item-agent",
        tool_name="get_order_items",
        evidence_refs=[items_ref] if items_ref else [],
    )
    for item in items_ev.get("data", []):
        if item.get("order_item_id") and item["order_item_id"] not in item_ids:
            item_ids.append(item["order_item_id"])
        if item.get("seller_id") and item["seller_id"] not in seller_ids:
            seller_ids.append(item["seller_id"])

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-item-agent",
        target="coordinator",
        decision_code="ORDER_EVIDENCE_COLLECTED",
    )

    # -------------------------------------------------------------
    # 3. Evidence-First Detection Logic
    # -------------------------------------------------------------
    detected_issue: str | None = None
    shipment_ids: list[str] = []
    payment_references: list[str] = []

    # Priority 1: Check Order Status
    if order_status == "canceled":
        detected_issue = "canceled_order_paid"
    elif order_status == "unavailable":
        detected_issue = "unavailable_order_paid"

    # Priority 2: Check Shipment Summary if not canceled/unavailable
    if not detected_issue:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            attributes={"task": "get_shipment_summary", "order_id": claimed_order_id},
        )
        ship_ev = await gateway.call(
            "get_shipment_summary", case_id=case_id, order_id=claimed_order_id
        )
        ship_ref = ship_ev.get("evidence_ref")
        record_evidence("shipment", ship_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment-agent",
            tool_name="get_shipment_summary",
            evidence_refs=[ship_ref] if ship_ref else [],
        )

        ship_events = ship_ev.get("data", {}).get("events", [])
        for ev in ship_events:
            if ev.get("event_type") == "delivered_late":
                actor = ev.get("actor")
                if actor == "seller":
                    detected_issue = "late_delivery_seller"
                    break
                elif actor == "logistics_provider":
                    detected_issue = "late_delivery_logistics"
                    break

        if (
            detected_issue == "late_delivery_seller"
            or customer_claimed_topic == "late_delivery_seller"
        ):
            sellers_ev = await gateway.call(
                "get_sellers", case_id=case_id, order_id=claimed_order_id
            )
            sellers_ref = sellers_ev.get("evidence_ref")
            record_evidence("seller", sellers_ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment-agent",
                tool_name="get_sellers",
                evidence_refs=[sellers_ref] if sellers_ref else [],
            )
            for s in sellers_ev.get("data", []):
                if s.get("seller_id") and s["seller_id"] not in seller_ids:
                    seller_ids.append(s["seller_id"])

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
            decision_code="SHIPMENT_EVIDENCE_COLLECTED",
        )

    # Priority 3: Check Payments, Payment Timeline & Refund Timeline
    if not detected_issue or detected_issue in ("canceled_order_paid", "unavailable_order_paid"):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
            attributes={"task": "get_order_payments", "order_id": claimed_order_id},
        )
        pay_ev = await gateway.call(
            "get_order_payments", case_id=case_id, order_id=claimed_order_id
        )
        pay_ref = pay_ev.get("evidence_ref")
        record_evidence("payment", pay_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-agent",
            tool_name="get_order_payments",
            evidence_refs=[pay_ref] if pay_ref else [],
        )
        for idx, p in enumerate(pay_ev.get("data", [])):
            p_seq = str(p.get("payment_sequential", idx + 1))
            if p_seq not in payment_references:
                payment_references.append(p_seq)

        if not detected_issue:
            # Check refund timeline first
            try:
                ref_ev = await gateway.call(
                    "get_refund_timeline", case_id=case_id, order_id=claimed_order_id
                )
                ref_ref = ref_ev.get("evidence_ref")
                record_evidence("refund", ref_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor="payment-agent",
                    tool_name="get_refund_timeline",
                    evidence_refs=[ref_ref] if ref_ref else [],
                )
                for rev in ref_ev.get("data", {}).get("events", []):
                    if rev.get("event_type") == "refund_requested":
                        r_status = rev.get("status")
                        if r_status == "failed":
                            detected_issue = "refund_failed"
                            break
                        elif r_status == "pending":
                            detected_issue = "refund_pending"
                            break
            except Exception:
                pass

        if not detected_issue:
            # Check payment timeline
            time_ev = await gateway.call(
                "get_payment_timeline", case_id=case_id, order_id=claimed_order_id
            )
            time_ref = time_ev.get("evidence_ref")
            record_evidence("payment", time_ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment-agent",
                tool_name="get_payment_timeline",
                evidence_refs=[time_ref] if time_ref else [],
            )
            t_events = time_ev.get("data", {}).get("events", [])
            for tev in t_events:
                if tev.get("event_type") == "reconciliation_mismatch":
                    detected_issue = "payment_mismatch"
                    break

            if not detected_issue:
                # Check for duplicate captures
                captured_events = [ev for ev in t_events if ev.get("event_type") == "captured"]
                amounts = [ev.get("amount_brl") for ev in captured_events]
                if len(amounts) > 1 and len(amounts) != len(set(amounts)):
                    detected_issue = "duplicate_charge"
                elif len(captured_events) > 1 and len(pay_ev.get("data", [])) > 1:
                    detected_issue = "valid_split_payment"

        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-agent",
            target="coordinator",
            decision_code="PAYMENT_EVIDENCE_COLLECTED",
        )

    # Fallback to unsupported_claim if no violation found
    if not detected_issue:
        detected_issue = "unsupported_claim"

    primary_issue = detected_issue

    # -------------------------------------------------------------
    # 4. Data Conflicts: detect discrepancy with customer claims
    # -------------------------------------------------------------
    data_conflicts: list[dict[str, Any]] = []
    if (
        customer_claimed_topic
        and customer_claimed_topic != "unsupported_claim"
        and customer_claimed_topic != primary_issue
    ):
        data_conflicts.append(
            {
                "field": "customer_request.claims.topic",
                "sources": ["customer_claim", "mcp_evidence_gateway"],
                "selected_source": "mcp_evidence_gateway",
                "resolution_code": "GROUND_TRUTH_EVIDENCE_DECISION",
            }
        )

    # -------------------------------------------------------------
    # 5. Policy Agent: consult policy rules & arbitrate
    # -------------------------------------------------------------
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        attributes={"task": "get_policy", "policy_version": policy_version},
    )

    policy_ev = await gateway.call("get_policy", case_id=case_id, policy_version=policy_version)
    policy_ref = policy_ev.get("evidence_ref")
    record_evidence("policy", policy_ref)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="policy-agent",
        tool_name="get_policy",
        evidence_refs=[policy_ref] if policy_ref else [],
    )
    policy_rules: dict[str, Any] = policy_ev.get("data", {}).get("rules", {})

    rule: dict[str, Any] = policy_rules.get(primary_issue, {})
    case_status: str = rule.get("case_status", "no_action")
    recommended_action: str = rule.get("recommended_action", "document_no_action")
    refund_brl: float = float(rule.get("refund_brl", 0.0))

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary_issue.upper(),
        attributes={
            "primary_issue": primary_issue,
            "case_status": case_status,
            "refund_brl": refund_brl,
        },
    )

    # Responsible parties from policy rule
    responsible_parties: list[dict[str, Any]] = []
    for party in rule.get("responsible_parties", []):
        ptype = party.get("party_type", "unknown")
        if ptype == "seller":
            pid = seller_ids[0] if seller_ids else party.get("party_id")
            if pid and pid not in seller_ids:
                seller_ids.append(pid)
        else:
            pid = None
        responsible_parties.append({"party_type": ptype, "party_id": pid})

    if not responsible_parties:
        responsible_parties.append({"party_type": "unknown", "party_id": None})

    # Financial resolution lines
    refund_lines: list[dict[str, Any]] = []
    if refund_brl > 0:
        refund_lines.append(
            {
                "reason_code": primary_issue.upper(),
                "amount_brl": round(refund_brl, 2),
                "entity_id": claimed_order_id,
            }
        )

    # Calibrated confidence
    # 0.98 if strong corroborating evidence; 0.95 if unsupported/conflict
    confidence = 0.95 if (data_conflicts or primary_issue == "unsupported_claim") else 0.98

    # Claim assessments
    claim_assessments: list[dict[str, Any]] = []
    for claim in customer_claims:
        cid = claim.get("claim_id", "claim-unknown")
        topic = claim.get("topic", "")

        if topic == primary_issue:
            verdict = "supported" if primary_issue != "unsupported_claim" else "unsupported"
            claim_assessments.append(
                {
                    "claim_id": cid,
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": all_evidence_refs[:10],
                }
            )
        elif topic == "requested_full_refund":
            if primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
                verdict = "supported"
            elif refund_brl > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            claim_assessments.append(
                {
                    "claim_id": cid,
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": all_evidence_refs[:10],
                }
            )
        else:
            claim_assessments.append(
                {
                    "claim_id": cid,
                    "verdict": "unsupported",
                    "confidence": confidence,
                    "evidence_refs": all_evidence_refs[:10],
                }
            )

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [claimed_order_id] if claimed_order_id else [],
            "item_ids": item_ids[:20],
            "seller_ids": seller_ids[:20],
            "payment_references": payment_references[:20],
            "shipment_ids": shipment_ids[:20],
        },
        "claim_assessments": claim_assessments[:5],
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": all_evidence_refs[:30],
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": round(refund_brl, 2),
            "refund_lines": refund_lines[:10],
        },
        "resolution_actions": [recommended_action],
    }

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier-agent",
        decision_code="POLICY_DECISION_FINALIZED",
    )

    # -------------------------------------------------------------
    # 6. Verifier Agent: validate invariants & contracts
    # -------------------------------------------------------------
    assert output["case_id"] == case_id, "case_id mismatch"
    assert (
        output["assessment"]["primary_issue"] == primary_issue
    ), "assessment primary_issue mismatch"
    assert output["assessment"]["case_status"] in (
        "action_required",
        "no_action",
        "needs_investigation",
    )
    if output["assessment"]["case_status"] == "no_action":
        assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
        assert len(output["financial_resolution"]["refund_lines"]) == 0
    else:
        refund_sum = sum(
            line["amount_brl"] for line in output["financial_resolution"]["refund_lines"]
        )
        assert (
            abs(refund_sum - output["financial_resolution"]["recommended_refund_brl"]) < 1e-4
        ), "refund total mismatch"

    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"]:
            assert party["party_id"] in output["affected_entities"]["seller_ids"]

    # Validate output against official JSON Schema contract
    trace.contracts.validate_output(output, f"verifier:output:{case_id}")

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        decision_code="INVARIANTS_PASSED",
        evidence_refs=all_evidence_refs[:5],
    )

    return output
