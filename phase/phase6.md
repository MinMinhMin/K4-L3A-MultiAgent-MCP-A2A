PHA 6: ĐÓNG GÓI ZIP, NỘP BÀI & GITHUB (210 – 240 PHÚT)
1. Quy chuẩn cấu trúc tệp ZIP Submission Mới
Tệp submission.zip Bắt buộc chỉ chứa 3 thành phần tại thư mục gốc của ZIP (tuyệt đối không có thư mục bọc ngoài nha mọi người, hệ thống unzip ra chấm 0đ vì không đọc được vào trong):

submission.zip
├── manifest.json
├── trace.jsonl
└── outputs/
    ├── L3A_CASE_001.json
    ├── L3A_CASE_002.json
    └── ... (đủ đúng 100 files output)
Chép
Chi tiết 3 thành phần bắt buộc:
1. manifest.json: Khai báo metadata đợt thi theo chuẩn submission-manifest-v2.schema.json:
{
  "schema_version": "day09-submission-manifest-v2",
  "competition_id": "day09-multiagent-mcp-a2a",
  "variant_id": "l3a",
  "case_set_version": "v2.1",
  "output_schema_version": "day09-l3a-output-v2",
  "trace_schema_version": "day09-trace-event-v1",
  "generated_at": "2026-09-25T08:00:00Z",
  "client": {
    "name": "day09-student-starter",
    "version": "0.1.0"
  }
}
Chép
1. trace.jsonl: Toàn bộ nhật ký audit event đa tác tử, đặt ngay tại root của ZIP.
2. outputs/: Thư mục chứa đúng 100 file JSON kết quả cho từng case.
Lưu ý: - Không đưa mã nguồn src/, file .env, Team API Key, raw inputs hay debug logs vào ZIP.
2. Lệnh đóng gói tự động chuẩn hóa
Để tránh sai sót thủ công, sử dụng lệnh đóng gói tích hợp sẵn trong CLI day09:

day09 package --output dist/submission.zip
Chép
Pass Signal Pha 6: Console in ra: OK: .../dist/submission.zip Lệnh này đã tự động chạy bộ kiểm tra toàn diện: validate 100 outputs, validate trace events, build manifest V2, kiểm tra dung lượng và quét regex phát hiện leak API Key trước khi xuất file zip.
3. Nộp bài lên Competition Workspace
1. Vào trang nộp bài và upload file zip đó lên
2. Hệ thống queue vào và auto scoring, đợi kết quả
3. Mọi người dùng filter tìm nhóm mình nếu không top 10 scorer hêhe