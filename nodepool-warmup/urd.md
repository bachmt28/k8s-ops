# URD — Warm-up node pool bằng over-provisioning

Pool đích: `k8s-example-sbapi-1-9` — cluster `k8s-cluster-example`. Mục 1–8 viết cho pool này; pool
`k8s-example-sbapiv11` áp cùng cơ chế với tham số ở mục 10.

---

## 1. Mục tiêu

Lúc 07:00 các ngày làm việc, đảm bảo pool có sẵn tối thiểu **7 node Ready**, để job Flink được bật
lúc 08:00 chạy ngay thay vì chờ pool nở.

Cơ chế: bù số node còn thiếu bằng **placeholder pod** mang `PriorityClass` âm. Placeholder bị Flink
pod preempt ngay khi job thật cần chỗ; node đã Ready từ trước nên Flink pod được schedule trong vài
giây.

Nếu 07:00 pool đã có từ 7 node Ready trở lên thì không làm gì.

---

## 2. Số liệu sizing (đo 2026-09-22)

| Hạng mục | Giá trị |
|---|---|
| Node allocatable | CPU `7840m`, RAM `61975896Ki` (~59.1 GiB) |
| Thế cân bằng tài nguyên | **CPU-bound** — requests CPU 96%, RAM 55% |
| Taint pool | `ratio=1-9:NoSchedule` |
| Label pool | `cloud.google.com/gke-nodepool=k8s-example-sbapi-1-9` |
| PriorityClass hiện có | `observability-critical`, `system-cluster-critical`, `system-node-critical` — chưa có class âm |

Flag `cluster-autoscaler` (`kube-system`, `release-1.29.3-gke.1`) ảnh hưởng trực tiếp tới thiết kế:

```
--expendable-pods-priority-cutoff=-10
--scan-interval=5s
--scale-down-unneeded-time=10m
```

- `expendable-pods-priority-cutoff=-10` — pod có priority **thấp hơn** `-10` bị autoscaler coi là
  expendable: không kích scale-up, bị bỏ qua khi tính scale-down. Placeholder rơi xuống dưới ngưỡng
  này là warm-up mất tác dụng hoàn toàn mà không báo lỗi.
- `scale-down-unneeded-time=10m` — node trống không có pod nào bị thu hồi sau 10 phút. Đây là lý do
  placeholder không được dập về 0 ngay sau khi job đầu tiên vào.
- `scan-interval=5s` — autoscaler phản ứng với pod `Pending` trong 5 giây, nên vòng lặp của script
  phải ngắn hơn khoảng này càng nhiều càng tốt (xem mục 6).

---

## 3. Phạm vi

- Một node pool: `k8s-example-sbapi-1-9` (namespace job: `app-vvp-jobs`).
- Khung giờ: **07:00 → 09:00, thứ Hai đến thứ Sáu, trừ ngày lễ**. Ngoài khung này không giữ node.
- Ngày lễ lấy chung danh sách với scaler `exception-ontime` (`exception-ontime/files/holidays.txt`,
  nhánh `dt` — nhánh Jenkins đang chạy). Ngày lễ scaler hạ toàn bộ workload (`HOLIDAY_MODE=hard_off`),
  giữ ấm chỉ tốn VM. `apply.sh` sinh ConfigMap `nodepool-warmup-holidays` từ file đó; **sửa
  `holidays.txt` xong phải chạy lại `apply.sh`**, không thì warm-up vẫn chạy ngày lễ mới thêm.
- Không thay đổi cấu hình Ververica, `OnPremNodePool` hay `cluster-autoscaler`.
- Job gom node `check-ultilized.sh` (`resources/JenkinsFile`) chạy với `--include="basev"` và
  `--include="sbapiv"`; hai pattern này **không khớp** `sbapi-1-9`, nên không xung đột.
- Scaler `exception-ontime/scaler.JenkinsFile` quản namespace theo pattern `^app-.*`, tức **có bao
  gồm `app-node-warmup`**. Action `weekday_prestart` (07:10–08:05 ngày thường) đưa mọi workload
  trong vùng quản lên `DEFAULT_UP=1`, kể cả placeholder Deployment mà script vừa hạ về 0. Đã
  thêm `app-node-warmup` vào `exception-ontime/files/deny-ns.txt` trên nhánh `dt` (chưa có trên `main`);
  lượt 29/09 replicas không bị dựng lại, tức scaler đang đọc bản có dòng này.

---

## 4. Luồng nghiệp vụ

```
07:00 (T2–T6) — CronJob nodepool-warmup-start
      │
      ├─ Hôm nay có trong holidays.txt  →  thoát 0
      ├─ Đã qua HOLD_UNTIL (09:00)  →  thoát 0 (Job chạy lại muộn không kéo dài khung giữ ấm)
      ├─ N0 = số node Ready trong pool
      ├─ need = TARGET_NODES − N0
      │     need <= 0  →  log "pool da co <N0> node" và thoát 0
      │
      ├─ Scale placeholder Deployment lên `need` replicas
      │
      ├─ [PHA 1 — chờ nở] lặp mỗi 5s
      │     node Ready >= TARGET_NODES                →  sang PHA 2
      │     đủ placeholder Running mà chưa đủ node    →  bù thêm (TARGET_NODES − node Ready)
      │       ↳ placeholder có thể chui vào node cũ còn trống, nên đủ placeholder ≠ đủ node
      │     quá WAIT_SCALEUP_SECONDS (30 phút)        →  LOI: giữ ấm phần đã có, đánh dấu partial
      │
      ├─ [PHA 2 — giữ ấm, tới HOLD_UNTIL] lặp mỗi 5s
      │     H = số placeholder đã gán node (kể cả đang khởi động)
      │     H < replicas hiện tại  →  scale Deployment về đúng H
      │       ↳ Flink vừa preempt: nhường hẳn chỗ đó, KHÔNG đòi lại
      │       ↳ placeholder còn lại tiếp tục giữ các node trống chưa dùng
      │     replicas == 0  →  thoát
      │
      └─ Tới HOLD_UNTIL → scale placeholder về 0, thoát
            mã 0 = đạt · mã 2 = autoscaler không cấp đủ node (partial)

09:00 (T2–T6) — CronJob nodepool-warmup-stop
      └─ Scale placeholder Deployment về 0, vô điều kiện (lưới an toàn nếu script chết)
```

Hai điểm khác bản ý tưởng ban đầu:

- **Không dập về 0 khi job đầu tiên vào.** Dập sớm thì node trống còn lại bị thu hồi sau 10 phút,
  job bật lúc 08:30 lại phải chờ. Thay vào đó replicas được hạ **đúng bằng số vừa bị preempt**.
- **Điều kiện cắt không phải `Running <= 1`.** Ngưỡng cứng đó chỉ thoả khi Flink ăn gần hết đệm;
  một job nhỏ chỉ preempt 1 placeholder sẽ làm script treo tới hết giờ. Tín hiệu đúng là *bất kỳ
  placeholder nào bị preempt*, và phản ứng là hạ replicas chứ không phải thoát.

---

## 5. Tài nguyên K8s

Namespace: `app-node-warmup` — tách riêng để `ResourceQuota` của `app-vvp-jobs` không bị placeholder
ăn vào. Đã tạo, không đặt `ResourceQuota`/`LimitRange`.

Image pull secret: `nexus-repo-secret` (type `kubernetes.io/dockerconfigjson`) trỏ tới
`common-docker.example.com`, user `k8sdeploy`. Đã tạo, theo đúng tên mà các namespace `app-*` khác
đang dùng.

### 5.1 PriorityClass

```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: overprovisioning
value: -1
globalDefault: false
description: "Placeholder pod giu cho node trong. Bi moi workload thuc te preempt."
```

`value: -1` chứ không phải `-10`: thấp hơn priority mặc định `0` của Flink pod nên bị preempt, đồng
thời cao hơn `expendable-pods-priority-cutoff=-10` nên autoscaler vẫn tính placeholder khi quyết
định scale-up. Đặt đúng `-10` vẫn chạy (`-10` không "thấp hơn" `-10`) nhưng sát mép: hạ thêm một
bậc là warm-up chết câm.

### 5.2 Placeholder Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nodepool-warmup-placeholder
  namespace: app-node-warmup
spec:
  replicas: 0
  selector:
    matchLabels:
      app: nodepool-warmup-placeholder
  template:
    metadata:
      labels:
        app: nodepool-warmup-placeholder
    spec:
      priorityClassName: overprovisioning
      terminationGracePeriodSeconds: 0
      imagePullSecrets:
        - name: nexus-repo-secret
      nodeSelector:
        cloud.google.com/gke-nodepool: k8s-example-sbapi-1-9
      tolerations:
        - key: ratio
          operator: Equal
          value: "1-9"
          effect: NoSchedule
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  app: nodepool-warmup-placeholder
              topologyKey: kubernetes.io/hostname
      containers:
        - name: pause
          image: common-docker.example.com/pause:3.9
          resources:
            requests:
              cpu: "7000m"
              memory: "48Gi"
              ephemeral-storage: "1Gi"
            limits:
              cpu: "7000m"
              memory: "48Gi"
              ephemeral-storage: "1Gi"
```

Căn cứ từng field:

- `nodeSelector` + `tolerations`: **bắt buộc**. Thiếu toleration `ratio=1-9:NoSchedule` thì
  placeholder không vào được pool đích; thiếu `nodeSelector` thì nó nở nhầm `basev5` hoặc
  `sbapiv11`, tốn VM mà Flink vẫn chờ.
- `cpu: 7000m` (~89% allocatable `7840m`). Hai cận: không được sát allocatable vì DaemonSet đã
  chiếm một phần, pod sẽ không schedule được; cũng không được nhỏ, vì preempt xong phải giải phóng
  đủ chỗ cho Flink pod chui vào — request bé thì Flink vẫn `Pending` và autoscaler lại đẻ node mới.
  Mức này cũng chốt quan hệ **1 placeholder = 1 node** mà logic đếm ở mục 4 dựa vào.
- `memory: 48Gi`: dưới allocatable 59.1 GiB, cũng đủ lớn để chặn hai pod chung node. Pool là
  CPU-bound nên CPU mới là ràng buộc thật, RAM đặt kèm cho chắc.
- `podAntiAffinity` theo `hostname`: chốt cứng quan hệ 1-1 kể cả khi ai đó sửa request sau này.
- `terminationGracePeriodSeconds: 0`: preempt xong giải phóng chỗ ngay.
- Image lấy từ registry nội bộ `common-docker.example.com`, không kéo thẳng `registry.k8s.io`.
  Node mới sinh chưa có image trong cache containerd và egress ra Internet đi qua proxy McAfee Web
  Gateway — đây đúng kiểu lỗi đã làm node mới không join được (`FailedCreatePodSandBox` khi kéo
  `pause-amd64`).

### 5.3 RBAC

ServiceAccount `nodepool-warmup` (ns `app-node-warmup`).

ClusterRole — đọc node toàn cluster:

```yaml
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
```

Role trong `app-node-warmup`:

```yaml
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["get", "update", "patch"]
```

`deployments/scale` là subresource riêng — cấp quyền trên `deployments` không đủ để `kubectl scale`.

### 5.4 CronJob

Hai CronJob, cùng ServiceAccount `nodepool-warmup`, cùng `imagePullSecrets: [nexus-repo-secret]`,
cùng image `bitnami/kubectl` **ghim digest** + `imagePullPolicy: IfNotPresent`.

| | `nodepool-warmup-start` | `nodepool-warmup-stop` |
|---|---|---|
| schedule | `0 7 * * 1-5` | `0 9 * * 1-5` |
| timeZone | `Asia/Ho_Chi_Minh` | `Asia/Ho_Chi_Minh` |
| concurrencyPolicy | `Forbid` | `Forbid` |
| startingDeadlineSeconds | `300` | `600` |
| activeDeadlineSeconds | `8100` (khung 7200 + 15 phút dư) | `600` |
| backoffLimit | `2` | `3` |
| podFailurePolicy | mã thoát `2` → `FailJob`; `DisruptionTarget` → `Ignore` | — |
| ttlSecondsAfterFinished | `72000` (20 giờ) | `72000` |
| successful/failedJobsHistoryLimit | `3` / `3` | `3` / `3` |
| Nội dung | script mục 5.5 | `kubectl -n app-node-warmup scale deploy/nodepool-warmup-placeholder --replicas=0` |

Căn cứ các giá trị:

- **`activeDeadlineSeconds` phải dài hơn khung giữ ấm.** Script tự dừng ở `HOLD_UNTIL`; deadline chỉ
  là lưới an toàn khi script treo. Deadline bằng đúng độ dài khung (7200) thì Job bị giết đúng lúc giữ
  ấm xong, báo `DeadlineExceeded` dù warm-up chạy đúng — deadline tính từ lúc Job được tạo, gồm cả
  thời gian kéo image, còn khung giữ ấm tính từ lúc script chạy.
- **`backoffLimit` + `podFailurePolicy`.** Pod chết giữa chừng (node hỏng, bị đuổi) thì chạy lại:
  mốc kết thúc là giờ treo tường nên lần chạy lại vẫn dừng đúng 09:00. Mã `2` (autoscaler không cấp
  đủ node) thì dừng hẳn, chạy lại không giải quyết được gì. Pod bị preempt/đuổi (`DisruptionTarget`)
  không tính vào `backoffLimit`.
- **`ttlSecondsAfterFinished: 72000`.** Job lỗi còn tồn tại thì `KubeJobFailed` kêu mãi tới khi có
  người xoá. Tự dọn sau 20 giờ: alert hết trước lượt chạy sáng hôm sau, vẫn đủ thời gian đọc log
  trong ngày.
- **Image ghim digest + `IfNotPresent`.** Tag `latest` mặc định `imagePullPolicy: Always`, mỗi lần
  chạy đều gọi registry — node mất DNS tới registry là Job `ImagePullBackOff`. Tag `latest` của
  bitnami cũng có thể đổi/biến mất bất cứ lúc nào.
- **Stop 600 giây, `backoffLimit: 3`.** 120 giây từng không đủ khi cluster chậm (sự cố 28/09).

`spec.timeZone` là GA từ K8s 1.27, cluster đang 1.29 nên dùng được. Nếu field bị từ chối, đổi sang
giờ UTC: `0 0 * * 1-5` và `0 2 * * 1-5` — cron của CronJob mặc định chạy theo giờ của
`kube-controller-manager`, **không** phải giờ Việt Nam.

**Chạy warm-up ngoài lịch** — không cần sửa gì, tạo Job từ CronJob:

```sh
kubectl -n app-node-warmup create job --from=cronjob/nodepool-warmup-start \
  warmup-manual-$(date +%Y%m%d-%H%M)
```

Chạy ngoài khung 07:00–09:00 thì script thoát ngay vì đã qua `HOLD_UNTIL`. Muốn giữ ấm giờ khác,
tạo Job bằng `--dry-run=client -o yaml`, thêm `env` ghi đè `HOLD_UNTIL` (và `TARGET_NODES` nếu cần)
trước `envFrom`, rồi `kubectl apply`. Muốn dừng sớm thì chạy tương tự với `nodepool-warmup-stop`.

### 5.5 Logic script `warmup.sh`

```
đọc tham số  →  POOL, TARGET_NODES, NAMESPACE, DEPLOY, HOLD_UNTIL, WAIT_SCALEUP_SECONDS, INTERVAL

hold_end = HOLD_UNTIL hôm nay (theo TZ)
nếu now >= hold_end:  thoát 0

N0 = số node trong pool có cột STATUS đúng bằng "Ready" (loại NotReady và node bị cordon)
need = TARGET_NODES - N0
nếu need <= 0:  thoát 0

scale DEPLOY -> need ; current = need ; partial = 0

PHA 1 — chờ nở:
    mỗi INTERVAL giây:
        R = số pod placeholder Running ; N = số node Ready
        H = số pod placeholder đã gán node, không đang bị xoá
        nếu N >= TARGET_NODES:
            nếu H < current: scale DEPLOY -> H ; current = H   # bị preempt ngay lúc đang nở
            sang PHA 2
        nếu R >= current (đủ placeholder, thiếu node):
            current += TARGET_NODES - N ; scale DEPLOY -> current
        nếu quá WAIT_SCALEUP_SECONDS:
            log LOI ; scale DEPLOY -> H ; current = H ; partial = 1 ; sang PHA 2

PHA 2 — giữ ấm:
    mỗi INTERVAL giây:
        H = số pod placeholder đã gán node, không đang bị xoá
        nếu H < current:  scale DEPLOY -> H ; current = H      # nhường chỗ vừa bị preempt
        nếu current == 0:  thoát (partial ? 2 : 0)

tới hold_end:
    nếu vẫn ở PHA 1:  log LOI ; partial = 1
    scale DEPLOY -> 0 ; thoát (partial ? 2 : 0)
```

Ràng buộc khi hiện thực:

- `INTERVAL = 5` giây (xem mục 6, rủi ro node dư).
- Hai cách đếm placeholder, mỗi cách cho một quyết định:
  - **Hạ replicas** dựa trên `H` — pod có `spec.nodeName` và không có `deletionTimestamp`. Pod bị
    preempt bị xoá, pod thay thế nằm `Pending` chưa gán node, nên không được đếm. Pod vừa gán vào
    node mới còn đang pull image / chờ CNI thì vẫn được đếm. Đếm theo `Running` ở đây sẽ hạ nhầm
    chính placeholder vừa lên node mới (lỗi gặp khi kiểm thử 29/09, mục 7.3).
  - **Bù thêm** ở pha 1 dựa trên `R` (`Running`). Pod chỉ `Running` khi node đã sẵn sàng thật, nên
    không bù dư node khi node mới còn đang khởi động.
- Điều kiện xong pha 1 là **đủ node**, không phải đủ placeholder: Flink có thể preempt placeholder ngay
  trong lúc pool đang nở, pod bị preempt nằm `Pending` mãi.
- Mọi lệnh ghi chỉ tác động lên đúng `deployments/scale` của `nodepool-warmup-placeholder`. Script
  **không** được cordon, drain, delete node, delete machine hay chạm vào workload khác.
- Log ra stdout, mỗi dòng có timestamp, để đọc lại bằng `kubectl logs job/...`.

### 5.6 Tham số

Đặt trong ConfigMap `nodepool-warmup-config`, mount thành env.

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `POOL` | `k8s-example-sbapi-1-9` | Giá trị label `cloud.google.com/gke-nodepool` |
| `TARGET_NODES` | `7` | Số node Ready tối thiểu cần có trong pool. Không vượt `maxReplicas` của pool (9) |
| `NAMESPACE` | `app-node-warmup` | |
| `DEPLOY` | `nodepool-warmup-placeholder` | |
| `HOLD_UNTIL` | `09:00` | Giờ treo tường kết thúc giữ ấm. Khớp lịch `nodepool-warmup-stop` |
| `WAIT_SCALEUP_SECONDS` | `1800` | Quá hạn này mà pool chưa đủ node thì coi là autoscaler không cấp node |
| `INTERVAL` | `5` | Giây giữa hai vòng lặp |
| `TZ` | `<+07>-7` | Múi giờ cho `date` và log |
| `HOLIDAYS_FILE` | `/opt/holidays/holidays.txt` (mặc định trong script) | Mount từ ConfigMap `nodepool-warmup-holidays`, `optional: true`: thiếu ConfigMap thì log `CANH BAO` và chạy như ngày thường. Mỗi dòng một ngày `YYYY-MM-DD`, bỏ qua `#` và khoảng trắng |

**`TZ` phải là chuỗi POSIX offset, không phải `Asia/Ho_Chi_Minh`.** Image `bitnami/kubectl` không có
tzdata: tên vùng bị bỏ qua im lặng, `date` chạy giờ UTC, và `HOLD_UNTIL=09:00` thành 16:00 giờ Việt
Nam. `<+07>-7` là offset cố định, đúng cho Việt Nam vì không đổi giờ theo mùa.

---

## 6. Rủi ro

| Rủi ro | Xử lý |
|---|---|
| **Node dư.** Placeholder bị preempt thành `Pending`; autoscaler scan mỗi 5s nên có thể kịp bấm scale-up trước khi script hạ replicas. | Chấp nhận dư tối đa 1 node mỗi lần preempt; node dư rỗng nên bị thu hồi sau `scale-down-unneeded-time=10m`. `INTERVAL=5` để thu hẹp cửa sổ. |
| Placeholder không vào được pool do thiếu toleration/nodeSelector | Đã chốt cứng trong manifest 5.2, nghiệm thu kiểm tra trực tiếp. |
| Không kéo được image `pause` trên node mới | Lấy từ `common-docker.example.com` qua `nexus-repo-secret`, không đi Internet. |
| CronJob chạy sai giờ | `spec.timeZone: Asia/Ho_Chi_Minh`, nghiệm thu bằng `.status.lastScheduleTime`. |
| Script chết giữa chừng, placeholder nằm lại cả ngày | CronJob `nodepool-warmup-stop` 09:00 độc lập với script. |
| Scaler `exception-ontime` dựng lại placeholder sau khi script đã hạ về 0 | Loại `app-node-warmup` khỏi vùng quản của scaler bằng `deny-ns.txt`. |
| Autoscaler bị khoá (cluster không `READY`, mất resource pool vSphere...) nên pool không nở | Sau `WAIT_SCALEUP_SECONDS` script ghi `LOI`, giữ ấm phần đã có, Job kết thúc mã `2` → `KubeJobFailed`. Không chạy lại. |
| Job bị báo lỗi oan khi giữ ấm tới cuối khung | `activeDeadlineSeconds` dài hơn khung giữ ấm 15 phút; script tự dừng ở `HOLD_UNTIL`. |
| Node mất kết nối tới registry, Job `ImagePullBackOff` | Image ghim digest + `IfNotPresent`: node đã có image thì chạy không cần registry. |
| Múi giờ sai trong container (image không có tzdata) | `TZ` dạng POSIX `<+07>-7`, xem 5.6. |

---

## 7. Tiêu chí nghiệm thu

1. `kubectl get priorityclass overprovisioning` trả `VALUE = -1`, `GLOBAL-DEFAULT = false`.
2. Scale tay placeholder lên 1: pod `Running` **trên node thuộc pool `k8s-example-sbapi-1-9`**,
   không phải pool khác.
3. Scale tay lên `N`: sinh đúng `N` node, mỗi node đúng 1 placeholder (`kubectl get pod -o wide`
   không có hai pod trùng `NODE`).
4. CronJob `nodepool-warmup-start` có `.status.lastScheduleTime` rơi vào 07:00 giờ Việt Nam.
5. Chạy lúc pool đã sẵn ≥ 7 node Ready: job kết thúc `Completed`, replicas vẫn `0`, không sinh node
   nào.
6. Khi một Flink job được bật: Flink pod chuyển `Running` trong **dưới 10 giây**, và log job có
   dòng `preempted ->` trong vòng 5 giây kế tiếp.
7. Placeholder chưa bị preempt vẫn `Running` tới 09:00 — node trống không bị thu hồi giữa chừng.
8. Đúng 09:00, `replicas = 0`; sau đó không còn pod nào của `nodepool-warmup-*`.
9. Số node dư (rỗng, không placeholder, không Flink pod) tại mọi thời điểm không quá 1.
10. `kubectl create job --from=cronjob/nodepool-warmup-start ...` cho kết quả giống lượt theo lịch.

### 7.1 Đã kiểm chứng trên cluster (2026-09-22)

Mục 1, 2, 5, 6 và 10 đã đạt. Bằng chứng:

- **Nhánh "đã đủ node"** — chạy `create job --from=cronjob/nodepool-warmup-start` lúc pool có 8
  node Ready: job `Completed` sau 7 giây, log ra
  `pool k8s-example-sbapi-1-9 da co 8 node Ready (target 5), khong can warm-up`, replicas giữ `0`.
- **Toleration và nodeSelector đúng** — scale placeholder lên 1, `FailedScheduling` liệt kê 8 node
  của pool bị loại **chỉ vì `Insufficient cpu`/`Insufficient memory`**, không có mục
  `untolerated taint {ratio: 1-9}`. Taint đã được tolerate, pod chỉ thiếu chỗ.
- **Priority `-1` kích được autoscaler** — sự kiện
  `TriggeredScaleUp: pod triggered scale-up: [{MachineDeployment/default/k8s-example-sbapi-1-9 8->9 (max: 9)}]`.
  Xác nhận `-1` nằm trên `expendable-pods-priority-cutoff=-10`.
- **Node mới phục vụ được placeholder** — node thứ 9 `Ready` sau ~5 phút, placeholder `Running`
  trên đúng node đó, image `pause:3.9` kéo từ `common-docker.example.com`.
- **Placeholder vào node đã ấm sau 26 giây** — scale lại lên 1 khi node đã `Ready` sẵn: pod
  `Running` sau 26 giây, không phải chờ clone VM. Đây chính là phần thời gian mà warm-up cắt được.
- **Preemption chạy đúng** — thả một Deployment dummy không khai `priorityClassName` (priority `0`,
  cùng `nodeSelector` và toleration với Flink pod, request `4000m`/`8Gi`) vào pool đang bị
  placeholder chiếm: `Killing` placeholder → `Scheduled` pod dummy **1 giây sau** → `Started` thêm
  1 giây. Tổng từ lúc apply tới lúc container chạy là 12 giây, phần lớn là thời gian API server
  tạo Deployment. Image `pause` đã nằm sẵn trong cache containerd của node
  (`already present on machine`) nên không mất thời gian pull.
- **Pod placeholder thay thế không kéo thêm node** — ReplicaSet sinh pod mới, pod này nằm `Pending`
  vì pool đã chạm `max: 9`. Đây đúng trạng thái mà pha giữ ấm xử lý: script hạ `replicas` xuống
  bằng số placeholder `Running` nên pod `Pending` biến mất trước khi kịp kích scale-up.

**Lần pull image đầu trên node vừa sinh có thể fail:**
`Failed to pull image ... dial unix /run/containerd/containerd.sock: connect: no such file or directory`.
Node chưa boot xong, kubelet retry và thành công ở lần sau. Không phải lỗi registry hay pull
secret, không cần xử lý.

### 7.2 Lượt chạy theo lịch đầu tiên (2026-09-23)

Nghiệm thu mục 4 đạt: CronJob nổ đúng 07:00 giờ Việt Nam.

Diễn biến, giờ đã quy về giờ Việt Nam:

| Mốc | Sự kiện |
|---|---|
| 07:00:02 | Pool chỉ còn **1 node Ready**, `need = 4`, scale placeholder lên 4 |
| 07:10:49 | Đủ 5 node Ready và 4 placeholder `Running` — mất **11 phút** |
| 07:28:40 → 07:30:55 | Job Flink thật preempt lần lượt cả 4: `4 → 3 → 2 → 1 → 0` trong **2 phút 15 giây** |
| 07:30:56 | Script thoát vì `replicas = 0` |

Pool **co về 1 node qua đêm**, nên warm-up là cần thiết chứ không thừa.

**Hai điều chỉnh rút ra:**

1. **Log ghi giờ UTC.** Container không set `TZ`, log ra `00:00:02` trong khi CronJob chạy lúc
   07:00. Thêm `TZ: Asia/Ho_Chi_Minh` **không có tác dụng** vì image không có tzdata — đã đổi sang
   `<+07>-7` (mục 5.6, 7.3).
2. **Nhu cầu thật vượt 5 node, và hết đệm từ 07:31.** Script thoát khi `replicas = 0` nên từ 07:31
   trở đi không còn node ấm. Pool sau đó vẫn phải nở tiếp — node mới sinh lúc 07:41, 08:02, 08:32,
   mỗi lần là một pod ngồi chờ clone VM khoảng 5 phút, đúng thứ warm-up sinh ra để tránh. Đã nâng
   `TARGET_NODES` lên `7` và rút khung giữ ấm về 07:00–09:00 (`HOLD_SECONDS=7200`, CronJob
   `nodepool-warmup-stop` chuyển sang `0 9 * * 1-5`). Nhánh thoát sớm khi `replicas = 0` **vẫn
   giữ nguyên** — nếu vài ngày nữa vẫn thấy pod chờ clone VM sau khi đệm cạn thì mới đổi sang "bù
   lại cho đủ `TARGET_NODES`".
3. **Scaler `exception-ontime` dựng lại placeholder.** Deployment về `1` lúc ~07:48 dù script đã
   hạ về `0` lúc 07:30:55 — pod đó nằm `Pending`, kéo thêm node rồi giữ chỗ vô ích. Nguyên nhân là
   namespace `app-node-warmup` khớp pattern `^app-.*` của scaler (mục 3), không phải lỗi của script.

### 7.3 Hai ngày Job lỗi (2026-09-28, 2026-09-29)

**28/09 — autoscaler bị khoá.** Sự cố đĩa vSphere trả `EROFS` làm cluster mất trạng thái `READY`,
`cluster-autoscaler` ghi `user cluster is not ready, skipping all nodegroups` và không dựng node nào.
Pool đứng ở 1 node, script chờ suốt khung giờ, Job bị giết `DeadlineExceeded`. Job stop 09:00 cũng
lỗi: pod không kịp chạy trong 120 giây. Warm-up không làm gì được khi autoscaler bị khoá. Việc cần
sửa là **báo rõ** chứ không để Job chết vì deadline: thêm `WAIT_SCALEUP_SECONDS` và mã thoát `2`.

**29/09 — warm-up chạy đúng nhưng Job vẫn báo lỗi.** Dựng lại từ metrics:

| Giờ | Sự kiện |
|---|---|
| 07:00 | Pool 1 node, scale placeholder lên 6 |
| 07:10 | Pool 6 node Ready, 6 placeholder `Running` — thiếu 1 so với `TARGET_NODES=7` |
| 07:30–07:35 | Job thật preempt, placeholder 6 → 1 |
| 07:35–09:00 | 1 placeholder giữ ấm |
| 09:00 | Job bị giết `DeadlineExceeded` |

Nguyên nhân và cách sửa:

1. **`activeDeadlineSeconds` bằng đúng khung giữ ấm (7200).** Deadline tính từ lúc tạo Job, nên Job bị
   giết trước khi script kịp thoát. Sửa: script tự dừng ở `HOLD_UNTIL`, deadline dư 15 phút (mục 5.4).
2. **Thiếu 1 node.** Một placeholder chui vào node cũ còn trống. Sửa: pha 1 bù tới khi đủ số node
   (mục 5.5).
3. **`KubeJobFailed` kêu nhiều ngày.** Job lỗi giữ tới 5 bản lịch sử. Sửa: `ttlSecondsAfterFinished`.
4. **`TZ: Asia/Ho_Chi_Minh` chưa từng có tác dụng.** Log hai ngày đều ghi giờ UTC; image không có
   tzdata. Sửa: `TZ: "<+07>-7"` (mục 5.6). Với `HOLD_UNTIL` là giờ treo tường, lỗi này nếu để lại sẽ
   kéo khung giữ ấm tới 16:00.

Kiểm thử trên cluster sau khi sửa:

- Chạy Job ngoài khung (sau 09:00): thoát `0` ngay, log đúng giờ Việt Nam sau khi đổi `TZ`.
- Một node `basev5` mất DNS tới registry: Job test `ImagePullBackOff` với image `:latest`. Đổi sang
  ghim digest + `IfNotPresent`.
- Chạy `TARGET_NODES=8` khi pool 7 node, lúc đang thay Machine của `basev5`: autoscaler lại báo
  `user cluster is not ready`, pool không nở. **Thay bất kỳ Machine nào trong cluster cũng khoá
  autoscaler của mọi pool.** Lịch thay Machine chủ động (bảo trì, node còn khoẻ) cần tránh khung
  07:00–09:00. Node chết thật thì khác: node đó đã làm `unavailableReplicas >= 1` và khoá
  autoscaler ngay khi chết, nên thay Machine (tay hoặc machine-reaper) càng sớm càng gỡ khoá sớm,
  kể cả trong khung.
- Cluster Ready lại, `TARGET_NODES=8` khi pool 7 node: node mới Ready sau 8,5 phút. Đúng giây đó
  placeholder **đã gán vào node mới nhưng còn đang pull image**, chưa `Running`. Bản đếm theo
  `Running` coi là bị preempt hết, hạ về 0 và giết chính placeholder vừa lên — node mới không được
  giữ. Sửa: quyết định hạ replicas dựa trên số pod đã gán node (mục 5.5).
- Chạy lại sau khi sửa, `TARGET_NODES=9` khi pool 8 node: node thứ 9 Ready sau 9,5 phút, log
  `du 9/9 node, 1 placeholder da gan node (0 Running)` — placeholder được giữ, chạy tới hết khung,
  Job `Succeeded`. Pod trên node mới có ~1 phút `ImagePullBackOff` ảnh `pause` trước khi chạy.
- `TARGET_NODES=10` (trên trần `maxReplicas=9`), `WAIT_SCALEUP_SECONDS=120`: log `LOI ... autoscaler
  khong cap node`, giữ ấm phần đã có tới hết khung, thoát mã `2`. Job `Failed` với reason
  `PodFailurePolicy`, đúng 1 pod — không chạy lại.
- Một lần chạy khác trúng vSphere lỗi lúc clone VM (`CreateError ... Config is not available`):
  cluster mất Ready ~12 phút rồi controller tự thử lại được. Job hết khung ở pha 1, thoát mã `2`.

---

## 8. Việc cần làm

1. Đưa dòng `app-node-warmup` trong `exception-ontime/files/deny-ns.txt` từ nhánh `dt` lên `main`.
2. Theo dõi các lượt 07:00 sau khi sửa: Job `Completed`, log có dòng `du <n>/7 node`, không còn
   `DeadlineExceeded`.
3. Đối chiếu số node đỉnh trong khung (PromQL ở 9.1) với `TARGET_NODES=7`, chỉnh nếu lệch.
4. Nếu sau khi đệm cạn vẫn còn pod chờ clone VM trong khung 07:00–09:00, cho pha 2 bù lại đủ
   `TARGET_NODES` thay vì chỉ nhường (xem 7.2).
5. Không lên lịch thay Machine chủ động (bất kỳ pool nào) trong khung 07:00–09:00; thay Machine
   của node chết thì làm ngay — xem 7.3.
6. Theo dõi lượt 06:40 đầu tiên của `sbapiv11` (mục 10): thời gian clone khi dựng song song ~34 VM,
   số node lúc 07:10, còn pod nào chờ clone VM trong khung không.

---

## 9. Áp dụng cho node pool / khung giờ khác

Cơ chế không gắn với Flink hay `sbapi-1-9`. Bất kỳ workload nào **bật theo lịch biết trước** trên một
pool **co về gần 0 ngoài giờ** đều dùng được. Trường hợp mới chỉ cần đủ các đầu vào dưới đây.

### 9.1 Đầu vào

| # | Đầu vào | Lấy ở đâu | Đổ vào |
|---|---|---|---|
| 1 | Tên node pool | label `cloud.google.com/gke-nodepool` của node | `POOL`, `nodeSelector` placeholder |
| 2 | Taint của pool | `kubectl get nodes -l cloud.google.com/gke-nodepool=<pool> -o jsonpath='{.items[0].spec.taints}'` | `tolerations` placeholder |
| 3 | Trần số node của pool | `maxReplicas` trong `OnPremNodePool` | ràng buộc `TARGET_NODES` |
| 4 | Allocatable 1 node | `kubectl describe node` → Allocatable | request placeholder |
| 5 | Request DaemonSet trên 1 node | tổng request các pod DaemonSet trên node đó | request placeholder |
| 6 | Request pod workload lớn nhất | spec của workload | request placeholder |
| 7 | Giờ workload bắt đầu, giờ hết đợt bật | lịch nghiệp vụ | `schedule` start, `HOLD_UNTIL`, `schedule` stop |
| 8 | Thời gian dựng 1 node mới | đo: lúc node tạo → `Ready` (sbapi-1-9: 5–11 phút) | giờ chạy start |
| 9 | Số node đỉnh trong khung | PromQL bên dưới, lấy nhiều ngày | `TARGET_NODES` |
| 10 | Ngày chạy | lịch nghiệp vụ | `schedule` (thứ trong tuần) |

Số node đỉnh trong khung giờ, mỗi ngày một giá trị (sửa tên pool và khung giờ):

```promql
max_over_time(
  count(kube_node_status_condition{condition="Ready",status="true",node=~"<pool>-.*"} == 1)[2h:1m]
)
```

Chạy dạng `query_range` với `step=1d`, mốc thời gian là giờ kết thúc khung (ví dụ 09:00).

### 9.2 Ràng buộc giữa các đầu vào

- `TARGET_NODES` ≤ trần số node của pool (#3). Vượt trần thì pha 1 không bao giờ xong, Job kết thúc mã `2`.
- Request placeholder:
  - **cận trên** là allocatable (#4) trừ request DaemonSet (#5). Vượt cận này thì placeholder không schedule được.
  - **cận dưới** là request pod workload lớn nhất (#6). Nhỏ hơn thì preempt xong vẫn không đủ chỗ, workload vẫn `Pending`.
  - Dùng chiều tài nguyên mà pool đang thiếu (CPU-bound hay RAM-bound).
- Giờ chạy start ≤ giờ workload bắt đầu (#7) − thời gian dựng node (#8) − dự phòng. Với `sbapi-1-9`:
  08:00 − 11 phút − dự phòng ≈ 07:00.
- `WAIT_SCALEUP_SECONDS` > thời gian dựng node (#8) × vài lần, vì autoscaler dựng node lần lượt. Dùng 30 phút.
- `activeDeadlineSeconds` của start = (`HOLD_UNTIL` − giờ start) + 15 phút.
- `schedule` stop = `HOLD_UNTIL`.

### 9.3 Cố định cho mọi trường hợp

Không phải đầu vào, không chỉnh theo từng pool:

- PriorityClass `overprovisioning` value `-1`: phải cao hơn `--expendable-pods-priority-cutoff=-10`
  của cluster-autoscaler, dùng chung cho cả cluster.
- `podAntiAffinity` theo hostname: 1 placeholder = 1 node.
- Namespace `app-node-warmup` nằm trong `exception-ontime/files/deny-ns.txt`.
- `TZ`, image ghim digest, RBAC.

### 9.4 Mỗi trường hợp mới sinh ra gì

| Tài nguyên | Dùng chung | Riêng từng trường hợp |
|---|---|---|
| Namespace, PriorityClass, ServiceAccount, RBAC, ConfigMap script | ✓ | |
| ConfigMap tham số (`POOL`, `TARGET_NODES`, `HOLD_UNTIL`...) | | ✓ |
| Deployment placeholder (`nodeSelector`, `tolerations`, request) | | ✓ |
| CronJob start + stop (`schedule`, `envFrom` trỏ ConfigMap riêng) | | ✓ |

Mỗi trường hợp là một file manifest tự đủ phần riêng: `manifests.yaml` (kèm phần dùng chung) cho
`sbapi-1-9`, `manifests-sbapiv11.yaml` cho `sbapiv11`. Thêm trường hợp thì chép khối "riêng từng
trường hợp" thành file mới, đổi tên theo pool, thêm dòng `kubectl apply` vào `apply.sh`. Khi số trường
hợp đủ nhiều để việc chép tay dễ lệch thì đóng Helm chart, values là danh sách `pools:` gồm đủ 10 đầu
vào ở 9.1, một file values đầy đủ, không overlay.

---

## 10. Pool `k8s-example-sbapiv11`

Pool chạy phần lớn workload dev/test. Scaler `exception-ontime` (`weekday_prestart`) bật workload từ
07:10, rải rác tới khoảng 09:00. Qua đêm pool co về 2 node, giờ làm việc lên 36–37 node.

Không có warm-up, pool nở **từng node một**:

- Mỗi lượt autoscaler chỉ thêm đủ node cho số pod `Pending` lúc đó. Pod bật rải rác nên mỗi lượt chỉ
  thấy vài pod, thêm 1 node.
- Trong lúc node đó clone, `unavailableReplicas >= 1` khoá cả pool; pod bật trong khoảng này phải chờ
  node trước join xong mới được tính.

Kết quả là cứ 6–8 phút thêm một node (log 09/10/2026: 08:53, 09:01, 09:12, 09:27...). Placeholder bật
một lượt cả khối từ trước 07:10 nên autoscaler thấy toàn bộ nhu cầu cùng lúc và clone song song.

### 10.1 Đầu vào (đo 02–09/10/2026)

| # | Đầu vào | Giá trị |
|---|---|---|
| 1 | Tên node pool | `k8s-example-sbapiv11` |
| 2 | Taint | `app=non-system:NoSchedule` |
| 3 | Trần số node | `45` (annotation `cluster-api-autoscaler-node-group-max-size` của MachineDeployment) |
| 4 | Allocatable 1 node | CPU `15760m`, RAM `30642580Ki` (~29.2 GiB) — VM 16 CPU / 32 GB |
| 5 | Request DaemonSet / node | tối đa `508m` / `833Mi` |
| 6 | Pod lớn nhất | `camunda-8-zeebe` (`app-bpm-zeebe-uat`): `4000m` / `12Gi` |
| 7 | Giờ workload bật / hết đợt | 07:10 / ~09:00 |
| 8 | Thời gian dựng node | 5.5–16.4 phút (đo 21/09, 33 node) |
| 9 | Số node đỉnh trong khung | 34–36 lúc 09:00, 36–37 lúc 10:00 các ngày thường; T7/CN 11–12 |
| 10 | Ngày chạy | T2–T6 trừ ngày lễ — khớp `weekday_prestart` của scaler (thứ Sáu vẫn bật workload: 02/10, 09/10 pool lên từ 2 node) |

Request lúc đo: CPU 95%, RAM 87% — pool **CPU-bound**.

### 10.2 Tham số chọn

| Tham số | Giá trị | Căn cứ |
|---|---|---|
| `TARGET_NODES` | `36` | Đỉnh lúc 09:00. Phần trên đó autoscaler tự thêm, lúc ấy pod bật thưa |
| Request placeholder | `14000m` / `24Gi` | Dưới allocatable trừ DaemonSet (`15252m` / ~28.4 GiB), trên pod lớn nhất; đủ lớn để không lọt vào node cũ còn lẻ chỗ |
| `schedule` start | `40 6 * * 1-5` | 07:10 − 16.4 phút − dự phòng cho việc clone song song vài chục VM |
| `HOLD_UNTIL` / stop | `09:00` | Hết đợt bật |
| `activeDeadlineSeconds` | `9300` | 06:40 → 09:00 = 8400s, cộng 15 phút |

Hai pool cùng warm-up trong khung 07:00–09:00, dùng chung namespace `app-node-warmup`, PriorityClass,
ServiceAccount và ConfigMap script.
