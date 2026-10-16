# k8s-ops

Script và manifest vận hành cluster GDC on-prem: tắt/bật workload theo giờ, gom node, warm-up node
pool, PDB, kiểm kê. Mỗi thư mục một công cụ, chạy độc lập.

## Tra khi có chuyện

| Tình huống | Xem |
|---|---|
| Workload bị scale về 0 ngoài giờ mà không mong muốn / không tự bật lại | [exception-ontime — Xử lý sự cố](exception-ontime/readme.md#65-xử-lý-sự-cố) |
| Đăng ký workload chạy ngoài giờ (ngoại lệ) | [exception-ontime — Cách đăng ký](exception-ontime/readme.md#52-cách-đăng-ký-ngoại-lệ) |
| Node bị cordon / pod bị evict không rõ vì sao | [resources/check-ultilized.md](resources/check-ultilized.md) — job gom node |
| Job `nodepool-warmup-start` Failed (`KubeJobFailed` ns `app-node-warmup`) | Log Job; mã thoát 2 = autoscaler không cấp đủ node — [nodepool-warmup/urd.md 5.4](nodepool-warmup/urd.md#54-cronjob), các ca đã gặp ở [7.3](nodepool-warmup/urd.md#73-hai-ngày-job-lỗi-2026-09-28-2026-09-29) |
| Buổi sáng pool Flink nở chậm dù có warm-up | Cluster có Machine đang thay/kẹt không (khoá autoscaler mọi pool) — [nodepool-warmup/urd.md 7.3](nodepool-warmup/urd.md#73-hai-ngày-job-lỗi-2026-09-28-2026-09-29) |
| Drain node kẹt vì PDB (Machine xoá kẹt, autoscaler "user cluster is not ready") | CronJob `pdb-guard` (10 phút một lần) tự xoá PDB không bao giờ cho drain (đọc trạng thái: `ALLOWED 0` khi mọi pod đều khoẻ) và PDB trùng pod với PDB khác (eviction báo `more than one PodDisruptionBudget`), vá `unhealthyPodEvictionPolicy: AlwaysAllow` cho PDB tạo tay, trong `app-*`; xử lý ngay: `pdb/pdb-guard.sh` hoặc tạo Job từ CronJob. Chuẩn PDB: `maxUnavailable: 1` + `unhealthyPodEvictionPolicy: AlwaysAllow` |
| Cần warm-up cho node pool / khung giờ khác | [nodepool-warmup/urd.md mục 9](nodepool-warmup/urd.md#9-áp-dụng-cho-node-pool--khung-giờ-khác) |

Alert của cluster (node, đĩa, Machine, cụm monitoring) có runbook riêng ở repo `infra-observability`,
file `docs/runbook.md`.

## Công cụ

| Thư mục | Làm gì | Chạy bằng | Tài liệu |
|---|---|---|---|
| `exception-ontime/` | Tắt workload ngoài giờ và cuối tuần, giữ/bật workload có đăng ký ngoại lệ | Jenkins: `dedupe`, `scaler`, `register` | [readme.md](exception-ontime/readme.md) |
| `resources/` | Tìm node dùng ít tài nguyên, cordon + evict để autoscaler thu hồi | Jenkins `resources/JenkinsFile`, chạy từ nhánh `main` | [check-ultilized.md](resources/check-ultilized.md) |
| `nodepool-warmup/` | Giữ chỗ node pool `sbapi-1-9` bằng placeholder 07:00–09:00 T2–T6 | CronJob trong cluster, ns `app-node-warmup`; cài bằng `apply.sh` | [urd.md](nodepool-warmup/urd.md) |
| `pdb/` | `pdb-guard` — CronJob 10 phút một lần xoá PDB không bao giờ cho drain (`ALLOWED 0` khi mọi pod khoẻ), PDB trùng pod với PDB tạo trước nó, vá `AlwaysAllow` cho PDB tạo tay, trong `app-*`; `pdb-scout.sh`, `pdb-delete.sh` chạy tay | CronJob ns `k8s-ops-jobs`, cài bằng `pdb/apply-pdb-guard.sh` | Comment đầu `pdb-guard.sh` |
| `virtualservice/` | Xuất danh mục Istio VirtualService ra CSV | Jenkins `virtualservice/Jenkinsfile` | Comment đầu script |
| `custom-rbac-data/` | ClusterRole mẫu cho DataHub, Milvus, VVP 3.x | `kubectl apply` | Comment trong file |
| `inventory-audit/` | ServiceAccount + quyền đọc toàn cluster cho kiểm kê | `kubectl apply` | Comment trong file |

## Quan hệ giữa các công cụ

- `exception-ontime` scaler quản mọi namespace `^app-.*`, trừ danh sách
  `exception-ontime/files/deny-ns.txt`. Namespace có workload tự quản số replica (như `app-node-warmup`)
  phải nằm trong danh sách này: action `weekday_prestart` đưa mọi workload lên `DEFAULT_UP=1`, dựng
  lại placeholder mà warm-up vừa hạ về 0.
- Job gom node (`resources/`) và autoscaler cùng làm co cluster; một `Machine` xoá kẹt vì PDB khoá
  autoscaler toàn cluster — `pdb-scout.sh` là chỗ tìm PDB gây kẹt.
