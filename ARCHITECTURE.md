# L3A Architecture Record

Tài liệu này mô tả kiến trúc đang được triển khai trong `src/student_agent`. Nội dung chỉ ghi các quyết định và sự kiện có thể kiểm chứng; không lưu prompt bí mật, API key hoặc chain-of-thought.

## 1. Mục tiêu và nguyên tắc thiết kế

Hệ thống điều tra khiếu nại thương mại điện tử bằng dữ liệu có thẩm quyền từ MCP Evidence Gateway, sau đó tạo output và observable trace đúng public contract.

Các nguyên tắc chính:

- Customer request là claim cần kiểm chứng, không phải ground truth.
- Chỉ dùng evidence do MCP trả về và không tự tạo `evidence_ref`.
- Mỗi specialist chỉ gọi nhóm tool thuộc phạm vi trách nhiệm của mình.
- Quyết định nghiệp vụ là deterministic rule engine; phiên bản hiện tại không dùng LLM.
- Không suy đoán dữ liệu khi evidence thiếu hoặc không hợp lệ.
- Output chỉ được ghi sau khi qua verifier và JSON Schema validation.

## 2. System overview

```text
case-set.json + inputs/<case_id>.json
                 │
                 ▼
         CLI / Coordinator
                 │
                 ├── Order/item agent ───── get_order, get_order_items
                 ├── Payment/refund agent ─ get_order_payments,
                 │                           get_payment_timeline,
                 │                           get_refund_timeline (có điều kiện)
                 ├── Shipment agent ──────── get_shipment_summary,
                 │                           get_sellers (có điều kiện)
                 └── Policy agent ────────── get_policy
                              │
                              ▼
                    Deterministic decision
                              │
                              ▼
                           Verifier
                         ┌────┴────┐
                         ▼         ▼
              outputs/<case>.json  traces/trace.jsonl
```

`day09 run` thực hiện các bước sau:

1. Nạp và validate case set, cấu hình và contract.
2. Xóa output và trace cũ để một lần chạy không trộn evidence của run khác.
3. Mở một MCP session, discovery tool và từ chối tiếp tục nếu server không trả tool.
4. Xử lý tuần tự từng `case_id` theo thứ tự trong `case-set.json`.
5. Coordinator tạo `CaseContext`, gọi lần lượt các specialist và nhận các report bất biến.
6. Rule engine hợp nhất report thành một `Decision` theo thứ tự ưu tiên cố định.
7. Verifier kiểm tra invariant; CLI tiếp tục validate output bằng JSON Schema.
8. Ghi file qua `.json.tmp` rồi atomic replace, sau đó emit `case_finalized`.

Xử lý tuần tự được chọn để giữ correlation đơn giản, tránh ghi đồng thời vào một trace file và không trộn provenance giữa các case.

## 3. Agent ownership

| Actor | Input | Tool được phép gọi | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | Raw case, `case_id`, gateway và trace writer | Không gọi trực tiếp domain tool | Validate input tối thiểu; điều phối DAG; tổng hợp report; ghi output | `CaseContext`, `case_received`, `task_assigned`, `case_finalized` |
| `order-item-agent` | `CaseContext` | `get_order`, `get_order_items` | Chuẩn hóa trạng thái order; item/seller ID; item và freight total; shipping limit; thời điểm bàn giao | `OrderReport`, handoff `ORDER_ITEMS_READY` |
| `payment-refund-agent` | `CaseContext` | `get_order_payments`, `get_payment_timeline`; `get_refund_timeline` chỉ cho topic `refund_pending`/`refund_failed` | Tổng hợp payment; phát hiện duplicate; xác định refund state và amount | `PaymentReport`, handoff `PAYMENT_REFUND_READY` |
| `shipment-agent` | `CaseContext`, `OrderReport` | `get_shipment_summary`; `get_sellers` chỉ khi seller bàn giao sau shipping limit | So sánh delivered/estimated; phân biệt seller delay và logistics delay; thu shipment ID | `ShipmentReport`, handoff `SHIPMENT_READY` |
| `policy-agent` | `CaseContext.policy_version` | `get_policy` | Nạp policy và payment tolerance; tham gia quyết định cuối | `PolicyReport`, handoff `POLICY_READY`, `policy_decided` |
| `verifier` | Context, output nháp và tập evidence đã consume | Không gọi MCP | Kiểm tra scope, evidence linkage, tiền, entity, action/status và confidence | `verification_completed` hoặc exception |

Hai tool được gateway cung cấp nhưng hiện không được gọi là `get_customer_history` và `get_product_context`. Các rule đang triển khai không cần hai nguồn này; không mở rộng quyền truy vấn nếu evidence không ảnh hưởng quyết định.

## 4. A2A protocol và observable trace

Hệ thống không dùng message broker. A2A được triển khai bằng lời gọi hàm async và các typed report nội bộ (`OrderReport`, `PaymentReport`, `ShipmentReport`, `PolicyReport`). Trace là giao thức quan sát công khai cho quá trình phối hợp.

Envelope của mỗi trace event tuân theo `day09-trace-event-v1`:

```json
{
  "schema_version": "day09-trace-event-v1",
  "event_id": "evt_<opaque-id>",
  "case_id": "L3A_CASE_001",
  "event_type": "task_assigned",
  "occurred_at": "<UTC timestamp>",
  "actor": "coordinator",
  "target": "order-item-agent",
  "decision_code": "COLLECT_ORDER_ITEMS"
}
```

Các trường `target`, `decision_code`, `tool_name`, `evidence_refs` và `attributes` chỉ xuất hiện khi phù hợp. Mọi event được validate trước khi append vào JSONL.

Chuỗi sự kiện cho một case:

```text
case_received
  → task_assigned → tool_result_consumed* → handoff       (order/item)
  → task_assigned → tool_result_consumed* → handoff       (payment/refund)
  → task_assigned → tool_result_consumed* → handoff       (shipment)
  → task_assigned → tool_result_consumed  → handoff       (policy)
  → policy_decided
  → verification_completed
  → case_finalized
```

- `case_id` là correlation key xuyên suốt input, MCP payload, trace và output.
- Specialist chỉ handoff sau khi các MCP response bắt buộc đã được validate và chuẩn hóa.
- `decision_code` là mã trạng thái quan sát được, không phải nội dung suy luận riêng.
- Workflow là DAG cố định, không có specialist-to-specialist recursion, nên không thể tạo vòng lặp A2A.
- Timeout nằm ở transport layer; không có timeout riêng cho từng agent.

## 5. MCP và evidence lifecycle

### 5.1 Thu thập

Mọi MCP call đi qua `EvidenceGateway.call`. Gateway tự thêm `case_id` vào payload và chỉ truyền các khóa định danh cần thiết như `order_id` hoặc `policy_version`.

Response hợp lệ phải có:

- `schema_version` đúng contract;
- `evidence_ref` theo mẫu `ev_...`;
- `result_hash` SHA-256;
- `domain` thuộc tập domain cho phép;
- `data` là payload từ nguồn có thẩm quyền.

Gateway ưu tiên structured content; nếu không có, chỉ chấp nhận đúng một text block chứa JSON. Evidence được validate bằng `mcp-evidence-response-v1.schema.json` trước khi trả cho specialist.

### 5.2 Consume và correlation

`_consume` thực hiện ba việc như một boundary thống nhất:

1. Gọi đúng tool với `context.case_id`.
2. Kiểm tra `evidence_ref` và `domain` ở mức workflow.
3. Emit `tool_result_consumed` với cùng `case_id`, actor, tool name và evidence ref.

Sau đó specialist chỉ giữ dữ liệu chuẩn hóa và các ref thật trong report. Rule engine chọn tập ref thực sự hỗ trợ kết luận; claim assessment chỉ được tham chiếu ref đã có ở top-level `evidence_refs`.

### 5.3 Scope và provenance

- Evidence không được cache hoặc tái sử dụng giữa các case.
- Mỗi case được xử lý trong cùng MCP run hiện tại và output cũ bị xóa trước khi chạy.
- Verifier từ chối mọi top-level evidence ref không thuộc tập ref đã consume cho case hiện tại.
- Không sửa, rút gọn hoặc tự sinh evidence ref.
- Tool tùy chọn chỉ được gọi khi claim hoặc evidence trung gian yêu cầu, tránh tạo lỗi “not applicable” và giảm dữ liệu không liên quan.

## 6. Decision policy

Rule engine đánh giá theo thứ tự ưu tiên sau; rule đầu tiên khớp sẽ tạo quyết định cuối:

1. Thiếu order status hoặc không có payment → `insufficient_evidence`.
2. Refund thất bại → `refund_failed`.
3. Refund đang xử lý → `refund_pending`.
4. Order canceled nhưng đã thanh toán → `canceled_order_paid`, hoàn toàn bộ payment.
5. Order unavailable nhưng đã thanh toán → `unavailable_order_paid`, hoàn toàn bộ payment.
6. Duplicate payment → `duplicate_charge`, hoàn phần duplicate xác định được.
7. Payment lệch item + freight quá policy tolerance → `payment_mismatch`.
8. Giao trễ do seller bàn giao sau shipping limit → `late_delivery_seller`, hoàn freight.
9. Giao trễ sau estimated date nhưng seller không bàn giao trễ → `late_delivery_logistics`, hoàn freight.
10. Có nhiều payment nhưng tổng tiền khớp → `valid_split_payment`, không hoàn tiền.
11. Không đủ timestamp shipment để xác định đúng/sai → `insufficient_evidence`.
12. Không có evidence hỗ trợ claim → `unsupported_claim`.

Giá trị tiền được tính bằng `Decimal`, làm tròn `ROUND_HALF_UP` đến `0.01 BRL`. Payment tolerance lấy từ policy; nếu field này không có trong một policy response hợp lệ, workflow dùng mặc định `0.10 BRL`.

## 7. Failure policy

| Failure | Retry | Fallback/hành vi | Observable trace/code |
| --- | --- | --- | --- |
| MCP transport error, gồm timeout hoặc server disconnect | Tối đa 3 lần tổng cộng; backoff `0.5s`, `1.0s` trước lần 2 và 3 | Retry cùng idempotent read request; hết retry thì re-raise và dừng run | Log `RETRY` ra stderr; trace dừng tại stage gần nhất vì schema chưa có failure event |
| MCP tool trả `is_error`, gồm not found/domain error | Không | Raise `RuntimeError`; không đổi thành evidence giả | Không emit `tool_result_consumed`; event gần nhất xác định specialist đang chạy |
| Tool trả success nhưng dataset rỗng | Không | Chuẩn hóa thành report thiếu dữ liệu; decision chuyển sang `insufficient_evidence` khi các field cốt lõi thiếu | Decision code `INSUFFICIENT_AUTHORITATIVE_EVIDENCE` hoặc `INSUFFICIENT_SHIPMENT_EVIDENCE` |
| Evidence sai schema, JSON không hợp lệ hoặc nhiều text block | Không | Raise `ValueError`/schema error và không tạo output case | Không emit handoff/finalize cho stage lỗi |
| Source conflict | Không tự retry | Phiên bản hiện tại chưa có conflict detector; `data_conflicts` luôn rỗng. Không được tự chọn dữ liệu phỏng đoán nếu bổ sung nguồn xung đột trong tương lai | Giới hạn đã biết của implementation hiện tại |
| Specialist report/output vi phạm invariant | Không | Verifier raise `ValueError`; CLI không ghi output cuối | Không có `verification_completed` hoặc `case_finalized` |
| Output không đạt public JSON Schema | Không | CLI dừng trước atomic write | Không có `case_finalized` |

HTTP client dùng timeout tổng/read `300s`, connect/write/pool `30s`. Retry chỉ áp dụng cho `httpx2.TransportError`; lỗi nghiệp vụ từ MCP không được retry vì lặp lại không làm thay đổi dữ liệu.

## 8. Verification invariants

Trước khi finalize, hệ thống kiểm tra:

- `output.case_id` bằng active `context.case_id`.
- Top-level `evidence_refs` là chuỗi hợp lệ, không trùng và là tập con của evidence đã consume.
- Evidence của từng claim là tập con của top-level evidence.
- Các danh sách order, item, seller, payment và shipment tồn tại và không chứa phần tử trùng.
- `recommended_refund_brl` không âm và bằng tổng tất cả refund line sau khi làm tròn 2 chữ số.
- Confidence là số trong đoạn `[0, 1]`.
- Case `no_action` phải có refund bằng 0.
- `canceled_order_paid` và `unavailable_order_paid` phải có status `action_required` và action `issue_full_refund`.
- `valid_split_payment` không được đề xuất refund.
- `resolution_actions` phải tồn tại, không rỗng và không trùng.
- Output hoàn chỉnh tiếp tục được validate bằng `l3a-output-v2.schema.json` trước khi ghi file.
- Mọi trace event được validate bằng `trace-event-v1.schema.json` trước khi append.

Atomic replace bảo đảm scorer không đọc một JSON mới chỉ được ghi một phần. `case_finalized` chỉ được emit sau khi file output đã được ghi thành công.

## 9. Reproducibility và vận hành

### 9.1 Runtime

- Python: `>=3.11`.
- Dependencies được giới hạn major version trong `pyproject.toml`: `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema[format]>=4.25,<5`, `python-dotenv>=1.1,<2`.
- Repo hiện không có lockfile, vì vậy tái lập tuyệt đối phiên bản package cần thêm bước freeze môi trường khi đóng gói nội bộ.
- Model/LLM: không sử dụng.
- Decision concurrency: `1` case tại một thời điểm, specialist chạy tuần tự.
- Random seed: không áp dụng cho quyết định. `event_id` dùng secure random và `occurred_at` dùng UTC hiện tại, nên trace metadata không byte-identical giữa các lần chạy.
- Output nghiệp vụ deterministic khi input, policy, MCP evidence và dependency behavior không đổi.

API key chỉ được đọc từ cấu hình môi trường và gửi dưới dạng Bearer header. Tài liệu, output và trace không chứa key.

### 9.2 Lệnh chuẩn

```powershell
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không chạy hai lệnh `day09 run` đồng thời trên cùng workspace vì cả hai cùng xóa/ghi `outputs/` và `traces/trace.jsonl`, đồng thời evidence của hai MCP run không được trộn.

### 9.3 Kết quả kiểm chứng gần nhất

Ngày 2026-09-25, một phiên chạy sạch đã hoàn tất với:

- `100/100` output;
- `1.874` trace event;
- event cuối là `case_finalized` cho `L3A_CASE_100`;
- `day09 validate` trả về `OK: 100 outputs / 1874 trace events`;
- process kết thúc với exit code `0`.

Kết quả này xác nhận contract, trace lifecycle và khả năng hoàn thành toàn bộ case set trong môi trường kiểm thử tại thời điểm chạy. Nó không thay thế đánh giá semantic/provenance của competition scorer.

## 10. Giới hạn đã biết

- Workflow chưa phát hiện và biểu diễn source conflict; `data_conflicts` hiện luôn rỗng.
- `get_customer_history` và `get_product_context` chưa được dùng vì chưa có rule cần hai nguồn này.
- Nếu run dừng giữa chừng, phải chạy lại từ đầu để giữ provenance nhất quán; chưa có checkpoint/resume an toàn theo MCP run ID.
- Trace schema chưa có event dành riêng cho retry hoặc failure; retry chỉ quan sát được qua stderr.
- Dependency chỉ pin theo khoảng version, chưa khóa exact build.
