PHA 2: THIẾT KẾ MULTI-AGENT A2A & CONTRACT SCHEMAS (30 – 65 PHÚT)
Mục tiêu:
Thiết lập kiến trúc phối hợp đa tác tử (A2A), phân định trách nhiệm từng Agent và khóa cứng Schema chuẩn trong contracts/schemas/.

Kiến trúc kỳ vọng:
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
Chép
Các nhiệm vụ trọng tâm:
1. Khóa Public Contracts (contracts/schemas/):
l3a-output-v2.schema.json (hoặc l3b): Schema bắt buộc cho output từng case.
trace-event-v1.schema.json: Schema cho trace log observable.
submission-manifest-v2.schema.json: Schema cho manifest khi đóng gói nộp bài.
mcp-evidence-response-v1.schema.json: Cấu trúc phong bì (envelope) trả về từ MCP Gateway.
Nguyên tắc: Không thêm bất kỳ field nào ngoài schema. Nếu có sai lệch, JSON Schema luôn là chân lý ưu tiên tối cao, tuyệt đối tuân thủ chỗ này nhé các bạn, ràng buộc đau đớn mà liêm
1. Linh hoạt framework các bạn là kỹ sư thiết kế thực chiến nên là thoải mái sáng tạo:
Điểm triển khai chính nằm tại: src/student_agent/workflow.py:
async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
    # Triển khai coordinator và specialist agents tại đây
    ...
Chép
Cuộc thi không chấm điểm dựa trên tên framework (học viên có thể dùng LangGraph, Semantic Kernel, CrewAI hoặc thuần Python async state-machine). Hệ thống chỉ đánh giá kết quả nghiệp vụ, tính hợp lệ của bằng chứng MCP và trace log.
1. Hoàn thiện mô tả kiến trúc trong ARCHITECTURE.md:
Phác thảo luồng handoff, tool permissions của từng agent, cơ chế retry khi MCP gặp sự cố.

