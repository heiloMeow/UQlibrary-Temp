概述
本服务是一个基于 UDP 协议的温度与投票数据采集服务器，通过 HTTP REST API 提供数据查询能力，并借助 SSE（Server-Sent Events）实现实时数据推送。适用于需要实时监控多设备温度反馈的场景。
基础信息
版本：2025-09-12
运行环境：Python 3.10+，依赖 aiohttp 库
端口占用：
UDP 监听：0.0.0.0:8080（接收设备上报数据）
HTTP 服务：0.0.0.0:5000（提供 API 与 SSE 服务）
跨域支持：默认开启全量 CORS（Access-Control-Allow-Origin: *），前端可直接跨域访问
快速开始
安装依赖：
bash
pip install aiohttp


启动服务：
bash
python temp_server.py


设备上报协议（UDP）
设备通过 UDP 协议向服务器上报数据，报文格式如下：
报文格式（必填字段）
plaintext
<uid_hex>:temp:<float>:vote:<int>

uid_hex：设备唯一标识（十六进制字符串）
temp:<float>：温度值（如 temp:26.44）
vote:<int>：温度感受投票，取值范围 {-1, 0, 1}，分别表示：
-1：寒冷（cold）
0：适宜（conf）
1：温暖（warm）
示例报文
plaintext
8813bf035bd8:temp:26.44:vote:1
上报说明
设备首次上报即完成 “隐式注册”，服务器自动记录设备 uid、时间戳、来源 IP 和端口
历史记录上限：单设备最多保留 200 条（HISTORY_MAX=200）
在线状态判定：60 秒内有数据上报视为在线（EXPIRE_SEC=60）
数据字段约定
设备信息（适用于 /api/temps、SSE 快照）
json
{
  "uid": "8813bf035bd8",       // 设备唯一标识（十六进制）
  "temp": 26.44,               // 最新温度值
  "vote": 1,                   // 最新投票（-1/0/1）
  "vote_tag": "warm",          // 投票对应的标签（cold/conf/warm）
  "ts": 1726123501.12,         // 时间戳（秒级，含小数）
  "iso": "2025-09-12 14:25:01",// 格式化时间字符串
  "online": true,              // 在线状态（60秒内有上报则为true）
  "ip": "192.168.137.234",     // 设备IP地址
  "port": 2222                 // 设备端口
}
历史记录项（适用于 /api/temps/<uid>/history）
json
{
  "ts": 1726123440.10,         // 时间戳
  "temp": 26.28,               // 温度值
  "vote": 0,                   // 投票值
  "vote_tag": "conf"           // 投票标签
}
REST API 接口说明
1. 健康检查
URL：GET /api/health
说明：检查服务器是否正常运行
返回示例：
json
{ "ok": true, "time": 1726123456.78 }

2. 获取所有设备最新数据
URL：GET /api/temps
说明：返回所有设备的最新记录，按时间戳（ts）降序排列
返回示例：
json
{
  "devices": [
    { "uid": "8813bf035bd8", "temp": 26.44, "vote": 1, "vote_tag": "warm", ... },
    { "uid": "8c4f00287dc4", "temp": 24.1, "vote": -1, "vote_tag": "cold", ... }
  ]
}

3. 获取指定设备最新数据
URL：GET /api/temps/<uid>
参数：uid 为设备唯一标识（如 8813bf035bd8）
返回示例：
json
{ "uid": "8813bf035bd8", "temp": 26.44, "vote": 1, "vote_tag": "warm", ... }

4. 获取指定设备历史记录
URL：GET /api/temps/<uid>/history
参数：uid 为设备唯一标识
说明：返回设备的历史记录数组，按时间戳（ts）升序排列
返回示例：
json
{
  "uid": "8813bf035bd8",
  "history": [
    { "ts": 1726123440.10, "temp": 26.28, "vote": 0, "vote_tag": "conf" },
    { "ts": 1726123501.12, "temp": 26.44, "vote": 1, "vote_tag": "warm" }
  ]
}

5. 投票统计
URL：GET /api/vote_stats?window=600&per_uid=1
参数：
window：统计时间窗口（秒），默认 600 秒（10 分钟）
per_uid：是否返回单设备统计（1 或 true 时返回）
说明：统计指定时间窗口内的投票数据（注意：统计的是事件条数，非唯一设备数）
返回示例：
json
{
  "window": 600,
  "now": 1726123999.99,
  "total": { "warm": 3, "conf": 5, "cold": 2 },
  "per_uid": {
    "8813bf035bd8": { "warm": 1, "conf": 2, "cold": 0 },
    "8c4f00287dc4": { "warm": 2, "conf": 0, "cold": 1 }
  }
}

重要语义说明
/api/vote_stats 统计逻辑：total 和 per_uid 均按 “事件条数” 统计（同一设备在窗口内多次上报会被重复计数），而非 “唯一设备数”。
如何获取 “一机一票” 统计（前端实现方案）：
javascript
运行
const now = Date.now() / 1000;
const window = 600; // 10分钟窗口
const since = now - window;

// 获取所有设备最新数据
const res = await fetch('/api/temps').then(r => r.json());

// 统计窗口内的唯一设备投票
const buckets = { warm: 0, conf: 0, cold: 0 };
let deviceCount = 0;

for (const device of res.devices) {
  if (device.ts >= since) { // 仅统计窗口内的记录
    deviceCount++;
    if (device.vote_tag) {
      buckets[device.vote_tag]++;
    }
  }
}

// buckets 为“一机一票”统计结果，deviceCount 为参与设备数

SSE 实时推送
服务器通过 SSE 向前端实时推送设备数据，支持自动重连。
连接地址
plaintext
GET /api/sse
事件类型
snapshot：首次连接时推送所有设备的快照数据（与 /api/temps 返回格式一致）
plaintext
event: snapshot
data: {"devices":[{"uid":"8813bf035bd8",...},...]}

temp：设备数据更新时推送增量数据（单设备最新记录）
plaintext
event: temp
data: {"uid":"8813bf035bd8","temp":26.52,"vote":1,"vote_tag":"warm","ts":1726123562.03}

前端使用示例
javascript
运行
// 建立 SSE 连接
const sse = new EventSource('http://<server-ip>:5000/api/sse');

// 接收快照数据
sse.addEventListener('snapshot', (event) => {
  const data = JSON.parse(event.data);
  console.log('设备快照', data.devices);
});

// 接收实时更新
sse.addEventListener('temp', (event) => {
  const newData = JSON.parse(event.data);
  console.log('设备更新', newData.uid, '温度:', newData.temp);
});
跨域配置（CORS）
服务器默认返回以下跨域响应头，支持前端直接访问：

plaintext
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type
变更记录
强制 UDP 报文必须携带 vote 字段，旧格式不再兼容
所有接口返回数据新增 vote 和 vote_tag 字段（包括设备列表、单设备详情、历史记录及 SSE 推送）
新增 /api/vote_stats 接口，明确按 “事件条数” 统计的语义
补充前端 “一机一票” 统计的实现示例

通过以上接口和协议，可实现设备温度与投票数据的实时采集、查询和监控，满足多场景下的温度反馈分析需求。
