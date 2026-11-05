from pydantic import ValidationError

from app.providers import LanguageModel, ProviderError
from app.schemas import ModelRule


async def fetch_catalog(provider: LanguageModel):
    raw = await provider.models()
    valid, invalid = set(), 0
    for model_id in raw:
        try:
            rule = ModelRule(id=model_id, enabled=True)
            if rule.id != model_id:
                raise ValueError("model ID must not contain surrounding whitespace")
            valid.add(rule.id)
        except (ValidationError, ValueError):
            invalid += 1
    if raw and not valid:
        raise ProviderError("上游返回的模型 ID 全部无效，未覆盖已有模型配置。")
    warnings = [f"上游返回了 {invalid} 个空白或无效模型 ID，已排除。"] if invalid else []
    return {"models": sorted(valid), "warnings": warnings}
