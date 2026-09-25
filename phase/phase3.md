PHA 3: TRIỂN KHAI SPECIALIST AGENTS & MCP GATEWAY (65 – 110 PHÚT)
Mục tiêu:
Từng agent chuyên trách truy vấn bằng chứng có thẩm quyền qua MCP Evidence Gateway theo đúng scope của từng case và ghi nhận trace audit.

Nguyên tắc của MCP Gateway:
#	Nguyên tắc	Nếu vi phạm
1	Truyền đúng case_id cho mọi MCP call	Bị từ chối truy cập (403 Forbidden)
2	KHÔNG tự sinh hoặc sửa đổi evidence_ref	Hard Gate 0 điểm toàn bài
3	Chỉ trích dẫn evidence thực sự hỗ trợ kết luận	Bị trừ điểm thành phần Evidence Relevance
4	Ghi nhận event tool_result_consumed trong trace	Không được công nhận tính xác thực
5	Server lưu Audit độc lập (Hash, Latency, Status)	Bị phát hiện nếu giả mạo trace client
Đọc kĩ chỗ này để tránh được các lỗi nhé mọi người.

Triển khai cụ thể:
1. Gọi Tool qua Gateway:
# Lấy dữ liệu đơn hàng có thẩm quyền từ MCP
evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)
evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]
Chép
1. Ghi nhận Trace Event hợp lệ:
# Ghi nhận sự kiện tiêu thụ bằng chứng vào trace audit
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)