"""配置读写 Web API。"""

from __future__ import annotations

from astrbot.api.web import json_response, request

from ...kernel.constants import PLUGIN_NAME


async def web_config_save(host):
        """POST /config body={'default_mute_reason': 'xxx'} 保存默认禁言理由（即时生效）。"""
        payload = await request.json(default={})
        reason = str(payload.get("default_mute_reason", "")).strip()
        if not reason:
            return json_response({"ok": False, "message": "默认禁言理由不能为空"})
        if len(reason) > 50:
            return json_response({"ok": False, "message": "理由太长啦，50 字以内"})
        host._write_config_value("basic", "default_mute_reason", reason)
        try:
            host.config.save_config()
        except Exception as e:
            host.logger.warning(f"保存配置失败: {e}")
            return json_response({"ok": False, "message": f"配置写入失败: {e}"})
        host.logger.info(f"WebUI 更新默认禁言理由为: {reason}")
        return json_response({"ok": True, "default_mute_reason": reason})

async def web_config_schema(host):
        """GET /config/schema：返回设置页所需的 schema、当前生效值与可选模型。"""
        schema = host._conf_schema()
        groups = []
        for gkey, gval in schema.items():
            if not isinstance(gval, dict) or not isinstance(gval.get("items"), dict):
                continue
            items = []
            for ikey, ival in gval["items"].items():
                if not isinstance(ival, dict):
                    continue
                value = host._read_config_value(
                    (gkey, ikey), default=ival.get("default"), legacy_keys=(ikey,)
                )
                items.append({
                    "key": ikey,
                    "description": str(ival.get("description") or ikey),
                    "type": str(ival.get("type") or "string"),
                    "default": ival.get("default"),
                    "options": ival.get("options") or [],
                    "hint": str(ival.get("hint") or ""),
                    "special": str(ival.get("_special") or ""),
                    "value": value,
                })
            groups.append({
                "key": gkey,
                "description": str(gval.get("description") or gkey),
                "items": items,
            })
        return json_response({
            "ok": True,
            "plugin": PLUGIN_NAME,
            "groups": groups,
            "providers": host._provider_options(),
            # 这些配置在插件加载时读取，改完需要重载插件才生效，页面会提示
            "reload_required": {"member_event": ["enable_member_remove_notify", "member_remove_try_fetch_name"]},
        })

async def web_config_bulk_save(host):
        """POST /config/save：批量保存配置项并写入 AstrBot 配置文件。

        body: {"values": {"basic": {"enable_mute": true, ...}, ...}}
        兼容单字段写法 {"group": "basic", "key": "enable_mute", "value": true}
        """
        payload = await request.json(default={}) or {}
        schema = host._conf_schema()
        changes = payload.get("values") if isinstance(payload.get("values"), dict) else None
        if not changes and payload.get("group") and payload.get("key") is not None:
            changes = {str(payload["group"]): {str(payload["key"]): payload.get("value")}}
        if not isinstance(changes, dict) or not changes:
            return json_response({"ok": False, "message": "没有收到要保存的配置项"})

        applied: dict[str, dict] = {}
        errors: list[str] = []
        for gkey, kv in changes.items():
            gschema = schema.get(gkey)
            if not isinstance(gschema, dict) or not isinstance(gschema.get("items"), dict):
                errors.append(f"未知配置分组：{gkey}")
                continue
            if not isinstance(kv, dict):
                continue
            for ikey, raw in kv.items():
                ischema = gschema["items"].get(ikey)
                if not isinstance(ischema, dict):
                    errors.append(f"未知配置项：{gkey}.{ikey}")
                    continue
                value, err = host._coerce_config_value(ischema, raw)
                if err:
                    errors.append(f"{ischema.get('description') or ikey}：{err}")
                    continue
                host._write_config_value(gkey, ikey, value)
                # 清掉历史遗留的扁平同名 key，避免新旧两份值并存造成困惑
                if ikey in host.config and ikey != gkey:
                    try:
                        del host.config[ikey]
                    except Exception:
                        # noqa: silent-cleanup —— 解析/读取兜底，失败即跳过
                        pass
                applied.setdefault(gkey, {})[ikey] = value

        if not applied:
            return json_response({"ok": False, "message": "；".join(errors[:3]) or "没有可保存的改动"})

        try:
            host.config.save_config()
        except Exception as e:
            host.logger.warning(f"保存配置失败: {e}")
            return json_response({"ok": False, "message": f"配置写入失败: {e}"})

        # 日志类配置支持热更新，不必重启插件
        if "log" in applied:
            log_cfg = applied["log"]
            try:
                host.logger.reconfigure(
                    level=log_cfg.get("log_level"),
                    to_file=log_cfg.get("log_to_file"),
                    max_entries=log_cfg.get("log_max_entries"),
                    keep_days=log_cfg.get("log_keep_days"),
                    log_dir=host._plugin_data_dir(),
                    plugin_name=PLUGIN_NAME,
                )
            except Exception as e:
                host.logger.warning(f"日志配置热更新失败: {e}")

        count = sum(len(v) for v in applied.values())
        detail = "、".join(f"{g}.{k}" for g, kv in applied.items() for k in kv)
        host.logger.info(f"WebUI 更新配置 {count} 项：{detail}")
        return json_response({
            "ok": True,
            "applied": applied,
            "errors": errors,
            "count": count,
            "message": f"已保存 {count} 项配置" + ("；" + "；".join(errors[:2]) if errors else ""),
        })
