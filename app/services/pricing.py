"""
模型定价服务

根据各模型官方定价计算 Token 费用
价格单位：美元 / 1M tokens
"""

from typing import Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP

# 模型定价配置（美元 / 1M tokens）
# 数据来源：各厂商官方定价页面
MODEL_PRICING = {
    # Anthropic Claude 4.5 系列
    "claude-opus-4.5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-4.5": {"input": 3.0, "output": 15.0},
    
    # OpenAI GPT-5 系列
    "gpt-5.1": {"input": 1.25, "output": 10.0},
    "gpt-5.2": {"input": 1.75, "output": 14.0},
    
    # Google Gemini 3 系列
    "gemini-3-pro": {"input": 2, "output": 12.0},
    "gemini-3-pro-image-preview": {"input": 2, "output": 120.0},
    
    # 默认定价（未知模型）
    "_default": {"input": 1.0, "output": 2.0},
}


def normalize_model_name(model: str) -> str:
    """
    标准化模型名称，移除空格、转小写、统一分隔符
    
    例如：
    - "Claude Opus 4.5" -> "claudeopus4.5"
    - "gemini 3 pro" -> "gemini3pro"
    - "gpt-5.1" -> "gpt5.1"
    """
    if not model:
        return ""
    # 转小写，移除空格和连字符
    return model.lower().replace(" ", "").replace("-", "").replace("_", "")


def get_model_pricing(model: str) -> dict:
    """
    获取模型定价
    
    支持多种命名格式：
    - "claude-opus-4.5" (标准格式)
    - "Claude Opus 4.5" (带空格和大写)
    - "claudeopus4.5" (无分隔符)
    
    Args:
        model: 模型名称
        
    Returns:
        {"input": 输入价格, "output": 输出价格} (美元/1M tokens)
    """
    if not model:
        return MODEL_PRICING["_default"]
    
    # 精确匹配
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    
    # 标准化后匹配
    normalized = normalize_model_name(model)
    
    # 定义标准化后的模型名称映射
    normalized_mapping = {
        "claudeopus4.5": "claude-opus-4.5",
        "claudeopus45": "claude-opus-4.5",
        "claudesonnet4.5": "claude-sonnet-4.5",
        "claudesonnet45": "claude-sonnet-4.5",
        "gpt5.1": "gpt-5.1",
        "gpt51": "gpt-5.1",
        "gpt5.2": "gpt-5.2",
        "gpt52": "gpt-5.2",
        "gemini3proimagepreview": "gemini-3-pro-image-preview",
        "gemini3proimage": "gemini-3-pro-image-preview",
        "gemini3pro": "gemini-3-pro",
    }
    
    # 精确标准化匹配
    if normalized in normalized_mapping:
        return MODEL_PRICING[normalized_mapping[normalized]]
    
    # 包含匹配（按优先级从高到低）
    patterns = [
        ("claudeopus4", "claude-opus-4.5"),
        ("claudeopus", "claude-opus-4.5"),
        ("claudesonnet4", "claude-sonnet-4.5"),
        ("claudesonnet", "claude-sonnet-4.5"),
        ("gpt5.1", "gpt-5.1"),
        ("gpt51", "gpt-5.1"),
        ("gpt5.2", "gpt-5.2"),
        ("gpt52", "gpt-5.2"),
        ("gemini3proimage", "gemini-3-pro-image-preview"),
        ("gemini3pro", "gemini-3-pro"),
        ("geminipro", "gemini-3-pro"),
    ]
    
    for pattern, pricing_key in patterns:
        if pattern in normalized:
            return MODEL_PRICING.get(pricing_key, MODEL_PRICING["_default"])
    
    return MODEL_PRICING["_default"]


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int
) -> Tuple[str, str, str]:
    """
    计算调用费用
    
    Args:
        model: 模型名称
        input_tokens: 输入 token 数
        output_tokens: 输出 token 数
        
    Returns:
        (input_cost, output_cost, total_cost) 字符串格式，精确到小数点后6位
    """
    pricing = get_model_pricing(model)
    
    # 使用 Decimal 精确计算
    input_price = Decimal(str(pricing["input"]))
    output_price = Decimal(str(pricing["output"]))
    
    # 价格是每 1M tokens，所以除以 1000000
    input_cost = (Decimal(input_tokens) * input_price / Decimal("1000000"))
    output_cost = (Decimal(output_tokens) * output_price / Decimal("1000000"))
    total_cost = input_cost + output_cost
    
    # 格式化为字符串，保留6位小数
    def format_cost(cost: Decimal) -> str:
        return str(cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
    
    return format_cost(input_cost), format_cost(output_cost), format_cost(total_cost)


def format_cost_display(cost_str: str) -> str:
    """
    格式化费用显示
    
    Args:
        cost_str: 费用字符串
        
    Returns:
        格式化后的显示字符串，如 "$0.001234" 或 "< $0.000001"
    """
    if not cost_str:
        return "$0"
    
    cost = Decimal(cost_str)
    
    if cost == 0:
        return "$0"
    elif cost < Decimal("0.000001"):
        return "< $0.000001"
    elif cost < Decimal("0.01"):
        return f"${cost_str}"
    elif cost < Decimal("1"):
        return f"${cost:.4f}"
    else:
        return f"${cost:.2f}"


class PricingService:
    """定价服务"""
    
    @staticmethod
    def get_pricing(model: str) -> dict:
        """获取模型定价"""
        return get_model_pricing(model)
    
    @staticmethod
    def calculate(model: str, input_tokens: int, output_tokens: int) -> Tuple[str, str, str]:
        """计算费用"""
        return calculate_cost(model, input_tokens, output_tokens)
    
    @staticmethod
    def format_display(cost_str: str) -> str:
        """格式化显示"""
        return format_cost_display(cost_str)
    
    @staticmethod
    def get_all_pricing() -> dict:
        """获取所有模型定价"""
        return {k: v for k, v in MODEL_PRICING.items() if k != "_default"}


# 单例
_pricing_service: Optional[PricingService] = None


def get_pricing_service() -> PricingService:
    """获取定价服务实例"""
    global _pricing_service
    if _pricing_service is None:
        _pricing_service = PricingService()
    return _pricing_service
