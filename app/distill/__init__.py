"""判定记录平台 —— 大模型当老师,为自训的 System 1 判官(Laya / Jev 类)和小模型(Qwen 等)
准备 on-policy 数据;同一套判官也作为 System 1 服务给其他 agent 用。

- teachers:老师 / 判官接入层(SDK)。三种协议:openai(Chat Completions)、anthropic(Messages API)、
  system_one(TypeSafe 的 System One API:Jev / laya-serve);用同一种"带类型的问题"提问、返回同一种概率格式。代码只内置 jev,
  其余全部来自配置(CE_TEACHERS_FILE / CE_TEACHERS,见 teachers.example.json)。
- records:每次 prime 的判定记录(原样请求、判官与参数、候选信号、最终推送)与事后结果,默认开启;
  影子判官(CE_SHADOW_JUDGES)在后台对同一请求作答,只记录不影响决定。
- archive:定期把判定、结果、老师标注追加归档成压缩 JSONL(长期保存);库里只删已归档的过期行。
- label:老师离线批量标注判定记录(System 2 复核 System 1)。
- calibrate:按真实答案校准每位老师的概率,多老师合成软标签,标出分歧大的难例。
- export:导出成 laya(typed-decisions 同构)/ kd(生成式小模型逐题训练样本)/ sft / pairs / retrieval / arena。
- arena:同一批判定交给多位判官,按真实答案出指标;gate:候选判官不输现任才给上岗建议(登记表)。
- gateway:给其他 agent 的 /api/v1/systemone(System One API 协议),由调用方代码或 hook 直接调用
  (System 1 要自动触发,不做成要模型主动调用的 MCP 工具);按路由表先问学生、拿不准再问老师,全量记录。

数据只存本机 PG;导出给外部模型或拿老师标注训练之前,由部署方确认隐私与各家服务条款。
"""
