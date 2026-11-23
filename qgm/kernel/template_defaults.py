"""用户可见文案的默认值（单一来源）。由模板审计生成，勿手改 schema 单独漂移。"""

TEMPLATE_DEFAULTS: dict[str, str] = {'msg_not_admin': '抱歉，你没有权限执行群管指令哦～',
 'msg_help': '### 群管指令速查\n'
             '\n'
             '写法：`/群管 子命令`，例：`/群管 禁言 @某人 10分钟 刷屏`。\n'
             '\n'
             '#### 审核与记录\n'
             '- `/群管 举报`：引用违规消息后发送\n'
             '- `/群管 记录 [判定|禁言|撤回|踢人] [@人] [N|今天|7天]`\n'
             '- `/群管 记录 我的 [类型] [N]`：普通成员查自己的记录\n'
             '- `/群管 记录 详情 V12`\n'
             '- `/群管 记录 汇总 [今天|7天]`\n'
             '\n'
             '#### 禁言与踢人\n'
             '- `/群管 禁言 @某人 10分钟 理由`\n'
             '- `/群管 禁言 解除 @某人`（可多人）\n'
             '- `/群管 禁言 名单`\n'
             '- `/群管 踢人 @成员… [理由]`\n'
             '\n'
             '#### 审批与群规\n'
             '- `/群管 审批` / `/群管 审批 通过 1` / `/群管 审批 拒绝 1 理由`\n'
             '- `/群管 群规` / `/群管 群规 添加 名称 | 说明 | 时长`\n'
             '\n'
             '#### 状态与维护\n'
             '- `/群管 状态` / `/群管 状态 自检` / `/群管 状态 群`\n'
             '\n'
             '> 仅记录本插件执行的操作；其他管理员的撤回/禁言不在此列。',
 'msg_api_error': '调用 QQ 接口失败：{error}',
 'msg_mute_success': '**已禁言** {username} · {duration_text}\n- 理由：{reason}',
 'msg_unmute_success': '**已解除** {username} 的禁言',
 'msg_mute_need_at': '请 @ 要禁言的用户，例如：/群管 禁言 @某人 10分钟',
 'msg_duration_invalid': '禁言时长格式不对哦，支持：30秒 / 5分钟 / 2小时 / 1天 或纯数字（秒）',
 'msg_mute_self': '不能禁言自己哦～',
 'msg_mute_failed': '禁言失败：{error}',
 'msg_mute_list': '### 当前被禁言成员\n\n{list}',
 'msg_mute_list_empty': '当前群内没有成员处于禁言状态',
 'msg_mute_history_list': '### 禁言历史\n\n最近 {count} 条（最新在前）：\n{list}',
 'msg_mute_history_summary': '### 禁言历史 · 汇总\n\n共 {count} 条记录：\n{list}',
 'msg_mute_history_empty': '本群还没有禁言历史记录',
 'msg_mute_history_user': '### 禁言历史 · {openid}\n\n最近 {count} 条：\n{list}',
 'msg_mute_history_user_summary': '### 禁言历史 · {openid} 汇总\n\n共 {count} 条：\n{list}',
 'msg_mute_history_user_empty': '未找到用户 {openid} 的禁言记录',
 'msg_recall_ok': '✅ **已撤回该消息**',
 'msg_recall_failed': '撤回失败：{error}',
 'msg_recall_invalid_id': '撤回失败：该消息的引用索引无法被撤回接口识别。若消息在 2 分钟内，请直接回复(引用)该消息后重试，机器人会按原文匹配真实消息 ID。',
 'msg_recall_need_quote': '### 没有可撤回的消息\n'
                          '\n'
                          '请长按目标消息 → 选择「回复」→ 发送 `/群管 撤回`。\n'
                          '\n'
                          '管理员如需按 ID 撤回，可在面板「插件处理记录」查看消息 ID。',
 'msg_recall_timeout': '撤回失败：该消息发送已超过 2 分钟，无法撤回',
 'msg_recall_no_perm': '撤回失败：机器人没有操作权限（需为群管理员才能撤回普通成员消息）',
 'msg_recall_group_only': '撤回功能仅支持群聊场景',
 'msg_recall_disabled': '撤回功能已在配置中关闭，可在插件设置里重新开启。',
 'msg_requests_list': '### 待处理的入群申请\n\n{list}',
 'msg_requests_empty': '当前没有待处理的入群申请',
 'msg_request_index_invalid': '序号无效，请用 /群管 审批 查看当前待处理列表',
 'msg_approve_success': '已通过 {username} 的入群申请',
 'msg_reject_success': '已拒绝 {username} 的入群申请',
 'msg_approve_failed': '审批操作失败：{error}',
 'msg_join_request_notify': '### 新的入群申请\n\n{list}\n\n管理员可点下方按钮处理，或用 `/群管 审批` 查看。',
 'msg_welcome_after_approve': '🎉 欢迎 {username} 加入本群！',
 'msg_welcome_new_group': '',
 'msg_group_intro': '### 我已就位\n'
                    '\n'
                    '我是 {bot_name}，可以帮群主和管理员做群管：\n'
                    '\n'
                    '- **违规处理**：引用违规消息发 `/群管 举报`\n'
                    '- **记录查询**：`/群管 记录`，成员可查自己的 `/群管 记录 我的`\n'
                    '- **禁言/踢人**：`/群管 禁言`、`/群管 踢人`\n'
                    '- **入群审批**：`/群管 审批`\n'
                    '- **群规配置**：`/群管 群规`\n'
                    '\n'
                    '请群主为我开启「接收群全部消息」「主动发言」，并授予群管理员身份。',
 'msg_member_remove_notify': '👋 {username}（{short}）离开了本群{extra}',
 'msg_join_request_auto_approved': '✅ 新成员 {username} 的入群申请已由自动审批策略通过（策略 {strategy}）',
 'msg_join_request_button_ok': '✅ 已通过 {username} 的入群申请',
 'msg_join_request_button_rejected': '🚫 已拒绝 {username} 的入群申请',
 'msg_join_request_button_gone': '⚠️ 这条入群申请已处理或已失效（可能刚被其他人处理），无需重复操作',
 'msg_join_request_button_denied': '⛔ 你没有处理入群申请的权限，请让群主/管理员操作',
 'msg_join_request_button_failed': '❌ 处理 {username} 的入群申请失败：{error}',
 'msg_report_daily': '### 群管日报 · {date} {time}\n'
                     '\n'
                     '- **待处理入群申请**：{pending_count} 条\n'
                     '{list}\n'
                     '- **当前禁言**：{muted_count} 人',
 'msg_kick_usage': '### 踢人\n'
                   '\n'
                   '- **用法**：`/群管 踢人 @成员… [理由]`\n'
                   '- **记录**：`/群管 踢人 记录 [N]`\n'
                   '\n'
                   '需群主在手机 QQ 与 q.qq.com 同时开启「移除群成员」。',
 'msg_kick_too_many': '单次最多移除 {max} 人，请分批操作。',
 'msg_kick_success': '**已移除** {names}（记录 `#{id}`）',
 'msg_kick_fail': '踢人失败：{error}',
 'msg_kick_history_empty': '本群还没有踢人记录。',
 'msg_kick_history_title': '### 踢人记录\n\n最近 {count} 条：\n{lines}',
 'msg_kick_perm_owner': '### 踢人失败（40103012）\n'
                        '\n'
                        '群主关闭了操作权限。\n'
                        '\n'
                        '1. 手机 QQ：群聊 → 机器人头像 → 右上角齿轮 → 机器人管理员权限设置\n'
                        '2. 勾选「移除群成员」\n'
                        '3. 重试',
 'msg_kick_perm_bot': '### 踢人失败（40103011）\n'
                      '\n'
                      '机器人侧关闭了操作权限。\n'
                      '\n'
                      '1. QQ 机器人 Web 管理端 → 群管理能力\n'
                      '2. 开启「移除群成员」\n'
                      '3. 在 q.qq.com 勾选对应能力后重试',
 'mod_hit': '**处理结果：已禁言 {duration}**\n\n- **依据**：{basis_detail}\n- **类别**：{category}',
 'mod_clean': '根据多重维度评判，此消息不涉嫌违规',
 'mod_hit_general': '**处理结果：已禁言 {duration}**\n\n- **类别**：{category}\n- **依据**：多重维度评判',
 'mod_receiving': '已收到举报，正在审核中…',
 'mod_failed': '审核未完成（{error}），已记入审核记录',
 'mod_no_quote': '请先长按违规消息选择「回复」引用它，再发送 /群管 举报',
 'mod_disabled': '本群未开启举报审核',
 'mod_dry_run': '**试运行（未处罚）**\n'
                '\n'
                '- **依据**：{basis_detail}\n'
                '- **类别**：{category}\n'
                '- **本应禁言**：{duration}',
 'mod_need_admin': '只有管理员可以修改群规',
 'mod_rules_empty': '本群还没有配置群规，审核暂时只按关键词和语义判断。管理员可以这样加一条：\n'
                    '/群管 群规 添加 刷屏 | 连续刷无意义内容 | 10分钟\n'
                    '- **（竖线隔开三段**：规则名 | 说明 | 时长；时长不写默认 1 小时）',
 'mod_report_limit': '举报太频繁啦，请稍后再试。',
 'mod_layer_off': '本群已关闭模型判定（仅关键词层），本次未命中词库，未做语义判定',
 'mod_uncertain': '判定把握不足，本次未自动处理，已记入审核记录',
 'mod_quote_lost': '没找到你引用的那条消息（可能太旧或已撤回），麻烦重新引用违规消息后再发一次 /群管 举报',
 'mod_exempt': '该成员在豁免名单中，本次不做处理',
 'mod_variant_recalled': '该消息疑似用变体写法绕过检测（{method}），已撤回并记录，请管理员复核',
 'mod_variant_logged': '该消息疑似用变体写法绕过检测（{method}），已记录待复核',
 'mod_variant_strike_notify': '{mentions} 疑似变体累计提醒：{user} 在近 {days} 天内已有 {count} '
                              '次疑似变体写法（本次：{method}）。已进待办，请到插件页面复核处理。',
 'mod_variant_strike_muted': '{mentions} 疑似变体累计已达 {count} 次（近 {days} 天），已自动禁言 {user} '
                             '{duration}。若判断有误，请解禁并在插件页面标记误判（标记后不再计入累计）。',
 'mod_admin_registered': '{user} 登记成功，已加入本群审核提醒名单（当前 {count} 人）。变体累计提醒会 @ 名单内成员。',
 'mod_admin_unregistered': '{user} 已从本群审核提醒名单移除（当前 {count} 人）。',
 'mod_admin_not_registered': '{user} 本来就不在审核提醒名单里（当前 {count} 人）。',
 'msg_mod_warn': '{at} **提醒：本消息已记录（警告，不撤回）**\n'
                 '\n'
                 '- **命中**：{rule}\n'
                 '- **判定**：{severity}\n'
                 '- **理由**：{reason}\n'
                 '- **后续**：再次出现将禁言 {next}\n'
                 '\n'
                 '有异议请联系群管理。',
 'msg_mod_todo': '### 需要人工复核\n\n这条内容疑似严重违规，但模型置信度不足，**暂不处罚**。\n\n- 已记入待办\n- 请到插件面板「群规审核 → 判定与试跑」复核',
 'msg_mod_revoke': '### 处罚撤销\n'
                   '\n'
                   '已撤销处罚记录 `#{vid}`。\n'
                   '\n'
                   '{at} 非常抱歉，LLM 审核对您做出了错误处罚。\n'
                   '\n'
                   '- **原处罚**：{punish}\n'
                   '- **禁言状态**：{unmute}\n'
                   '- **撤销人**：{operator}{reason_line}',
 'msg_mod_unattributed': '- **⚠️ 已判定**：{verdict}。\n'
                         '但这条消息不在消息缓存里（可能是机器人重载前发送、或当时没有收到），无法定位发送者，本次未处罚。需要处理的话请让当事人重新引用后举报。',
 'mod_admin_list': '本群审核提醒名单（{count} 人）：{admins}',
 'mod_admin_list_empty': '本群还没有人登记为审核管理员。管理员可发送「/群管 管理登记」把自己加入提醒名单。',
 'mod_admin_register_off': '提示：本群未开启「变体累计提醒」，登记已保存，但暂时不会收到 @（可在插件页面「举报设置」里打开）。',
 'mod_todo_muted': '已按待办处理：禁言 {user} {duration}',
 'mod_hit_semantic': '**处理结果：已禁言 {duration}**\n'
                     '\n'
                     '- **类别**：{category_public}\n'
                     '- **严重度**：{severity_label}\n'
                     '- **依据**：语义分析',
 'mod_dry_run_semantic': '**试运行（未处罚）**\n'
                         '\n'
                         '- **类别**：{category_public}\n'
                         '- **严重度**：{severity_label}\n'
                         '- **本应禁言**：{duration}'}
