PHA 4: POLICY ENGINE, VERIFIER & CALIBRATION (110 – 150 PHÚT)
Mục tiêu:
Áp dụng chính sách trọng tài tranh chấp, kiểm chứng chéo mâu thuẫn dữ liệu và hiệu chuẩn độ tin cậy.

Triển khai:
1. Policy Agent:
Ra quyết định dựa trên chính sách contracts/scoring/scoring-policy-v2.json:
Primary Issue: Xác định lỗi cốt lõi (canceled_order_paid, late_delivery_seller, late_delivery_logistics, payment_mismatch,...).
Responsible Party: Phân định trách nhiệm rõ ràng (seller, platform, logistics_provider, payment_provider, customer).
Financial Resolution: Số tiền hoàn chính xác (recommended_refund_brl, refund_lines).
Resolution Actions: Các hành động cụ thể cần thực hiện.
1. Verifier Agent & Confidence Calibration:
Cross-field Consistency: Đảm bảo primary_issue, responsible_parties và financial_resolution hoàn toàn logic và nhất quán (ví dụ: lỗi do người bán thì đơn vị vận chuyển không thể chịu trách nhiệm hoàn tiền).
Confidence Calibration: Đánh giá điểm tin cậy confidence [0.0 - 1.0] dựa trên chất lượng và độ đầy đủ của evidence (không tự tin thái quá 1.0 nếu bằng chứng có mâu thuẫn).
Lifecycle Events: Đảm bảo traces/trace.jsonl ghi nhận đủ các event bắt buộc:
case_received -> task_assigned -> tool_result_consumed -> handoff -> policy_decided -> verification_completed -> case_finalized