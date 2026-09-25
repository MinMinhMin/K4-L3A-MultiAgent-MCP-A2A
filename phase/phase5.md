PHA 5: TẢI INPUTS, BATCH RUN 100 CASES & XÁC THỰC (150 – 210 PHÚT)
Mục tiêu:
Tải bộ đề thi từ Github Release, chạy batch run toàn bộ cases và thẩm định tính toàn vẹn của outputs và trace log.

Quy trình thực hiện:
1. Tải và giải nén Test Bundle
1. Tải file ZIP input tương ứng (l3a-inputs-*.zip hoặc l3b-inputs-...) từ Github Release và giải nén vào thư mục inputs/ của repo, mọi người để đâu cho tiện là được không khống chế inpút :
unzip l3a-inputs-*.zip -d .
Chép
1. Cấu trúc sau khi giải nén:
case-set.json
inputs/
├── L3A_CASE_001.json
├── ...
└── L3A_CASE_100.json
Chép
1. Xác thực tính hợp lệ của tập input:
day09 validate-inputs
Chép
Pass Signal Pha 5.1: Terminal in ra thông báo hợp lệ, ví dụ: OK: l3a / v2.1 / 100 cases
2. Chạy Batch Run toàn bộ Cases
Thực thi pipeline xử lý toàn bộ cases thông qua CLI:

day09 run
Chép
Lệnh sẽ tự động lặp qua từng case trong case-set.json, gọi hàm solve_case trong workflow.py.
Tạo các tệp kết quả tại:
outputs/<case_id>.json (đủ 100 cases)
traces/trace.jsonl (toàn bộ dòng sự kiện timeline)
3. Thẩm định kết quả trước khi đóng gói
Kiểm tra tính tuân thủ Schema của toàn bộ outputs và trace:

day09 validate
Chép
Pass Signal Pha 5.2: Terminal in ra thông báo xác nhận: OK: 100 outputs / <N> trace events