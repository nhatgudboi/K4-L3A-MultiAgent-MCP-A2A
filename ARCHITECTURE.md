# L3A Architecture Record

Hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (E-Commerce Dispute Investigation) cho cuộc thi Day09 L3A Multi-Agent MCP + A2A.

## 1. System overview

Kiến trúc kỳ vọng theo mô hình phân tầng từ input đến MCP calls, specialist agents, policy engine, verifier, output và observable trace:

```text
┌────────────────────────┐
│  Coordinator / Router  │
└───────────┬────────────┘
            │ (Handoff)
    ┌───────┼───────┐
    ▼       ▼       ▼
┌──────┐ ┌─────┐ ┌────────┐
│Order/│ │Pay- │ │Shipment│
│Item  │ │ment │ │Agent   │
│Agent │ │Agent│ │        │
└──────┘ └─────┘ └────────┘
    │       │       │
    └───────┼───────┘
            │ (MCP Evidence Collector)
            ▼
     ┌──────────────┐
     │ Policy Agent │
     └──────┬───────┘
            │
            ▼
     ┌──────────────┐
     │Verifier Agent│
     └──────┬───────┘
            │ (Validated Output)
            ▼
       [END OUTPUT]
```

Luồng tương tác A2A song song với ghi nhận nhật ký Observable Trace:
`case_received` → `task_assigned` → `tool_result_consumed` → `handoff` → `policy_decided` → `verification_completed` → `case_finalized`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff | Công cụ được cấp quyền |
| --- | --- | --- | --- | --- |
| `coordinator` | `case_id`, `customer_request` | Nhận diện claim, phân định luồng nhiệm vụ cho specialist agents | Handoff sang Specialists / Policy Agent | Không gọi MCP Gateway trực tiếp |
| `order-item-agent` | `case_id`, `claimed_order_id` | Xác minh sự tồn tại, trạng thái đơn hàng (`order_status`), danh sách items và sellers | Order record, item IDs, seller IDs | `get_order`, `get_order_items` |
| `shipment-agent` | `case_id`, `claimed_order_id` | Xác minh timeline vận chuyển, sự kiện chậm trễ (`delivered_late`), định danh bên chịu lỗi | Shipment events, late actor, seller profile | `get_shipment_summary`, `get_sellers` |
| `payment-agent` | `case_id`, `claimed_order_id` | Điều tra thanh toán (duplicate charge, split payment, mismatch, refund failure) | Payment records, timeline events | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| `policy-agent` | `case_id`, `policy_version`, specialist evidence | Đối chiếu bằng chứng với chính sách có thẩm quyền, phân định lỗi, tiền hoàn, hành động | Rule evaluation, refund amounts, actions | `get_policy` |
| `verifier-agent` | Draft output, case constraints & audit trail | Thẩm định 8 invariants: schema contract, tính nhất quán tài chính, trách nhiệm seller, confidence | `decision_code: INVARIANTS_PASSED` | Internal Schema Validator & Assertions |

## 3. A2A protocol

- **Message Envelope**: Sử dụng chuẩn sự kiện `day09-trace-event-v1` ghi nhận cấu trúc: `case_id`, `event_type`, `actor`, `target`, `decision_code`, `tool_name`, `evidence_refs`, `attributes`.
- **Correlation**: Mọi tương tác A2A và MCP call được định danh duy nhất theo `case_id`. Tuyệt đối không trích dẫn hoặc tái sử dụng bằng chứng giữa các case khác nhau.
- **Handoff Conditions**:
  - `coordinator` -> `specialist`: phân công nhiệm vụ thu thập chứng cứ.
  - `specialist` -> `coordinator`: báo cáo sau khi gọi MCP tool và phát sự kiện `tool_result_consumed`.
  - `coordinator` -> `policy-agent`: chuyển giao toàn bộ chứng cứ để đối chiếu quy tắc bồi hoàn.
  - `policy-agent` -> `verifier-agent`: chuyển giao phương án giải quyết để thẩm định chéo độc lập.
- **Loop Prevention**: Workflow dạng DAG (Directed Acyclic Graph) một chiều, đảm bảo không có vòng lặp vô hạn.

## 4. Evidence lifecycle

1. **Query & Validation**: Specialist gọi gateway MCP (`session.call_tool`), tự động validate response envelope theo `mcp-evidence-response-v1.schema.json`.
2. **Audit Logging**: Ngay khi nhận evidence hợp lệ, specialist emit event `tool_result_consumed` với `evidence_refs=[evidence["evidence_ref"]]` và `tool_name`.
3. **Linkage**: Evidence refs được liên kết trực tiếp vào từng `claim_assessments` và tổng hợp vào mảng `evidence_refs` cấp case.
4. **Zero-Guessing**: Nghiêm cấm tạo hoặc sửa đổi `evidence_ref`. Nếu dữ liệu không tồn tại, phản ánh đúng hiện trạng (`unsupported` hoặc `insufficient_evidence`).

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP Timeout | Tối đa 2 lần với exponential backoff | Ghi nhận lỗi gateway | `decision_code: MCP_TIMEOUT` |
| Not Found / Tool Error | Không retry | Bỏ qua endpoint không tồn tại (ví dụ: refund timeline rỗng) | `decision_code: RESOURCE_NOT_FOUND` |
| Source Conflict | Không retry | Ưu tiên dữ liệu timeline có chứng thực từ hệ thống | `decision_code: CONFLICT_RESOLVED` |
| Invalid Specialist Result | 1 lần | Chuyển trạng thái `needs_investigation` | `decision_code: INVALID_SPECIALIST_OUTPUT` |

## 6. Verification invariants

Trước khi xuất file `outputs/<case_id>.json`, Verifier kiểm tra bắt buộc 8 điều kiện tiên quyết:
1. **Schema Compliance**: Output phải pass hoàn toàn JSON Schema `l3a-output-v2.schema.json` (Draft 2020-12).
2. **Entity Scope**: `case_id` và `order_id` phải khớp tuyệt đối với input của case.
3. **Evidence Provenance**: Mọi `evidence_ref` trong output đều phải có nguồn gốc từ gateway call của chính case đó.
4. **Financial Consistency**: Tổng `amount_brl` trong `refund_lines` phải khớp đúng `recommended_refund_brl`.
5. **Status & Action Match**:
   - Nếu `case_status == "no_action"`, `recommended_refund_brl` phải bằng `0.0` và `refund_lines` rỗng.
   - Nếu `case_status == "action_required"`, hành động giải quyết và số tiền phải tuân thủ nghiêm ngặt quy tắc trong `get_policy`.
6. **Seller Responsibility**: Nếu bên chịu trách nhiệm có `party_type == "seller"`, `party_id` phải thuộc danh sách `seller_ids` của đơn hàng.
7. **Claim Linkage**: Mọi `claim_id` trong customer request đều phải được đánh giá với verdict hợp lệ (`supported`, `partially_supported`, `unsupported`).
8. **Calibration**: Mức độ tin cậy `confidence` được hiệu chuẩn chính xác trong khoảng `[0.0, 1.0]`.

## 7. Reproducibility

- **Runtime & Dependencies**: Python 3.11+ (Python 3.13), `httpx2`, `mcp`, `jsonschema[format]`, `python-dotenv`.
- **Concurrency**: Xử lý tuần tự kết nối với MCP Gateway server.
- **Execution Command**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
