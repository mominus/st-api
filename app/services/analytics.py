"""
Analytics Service
调用后端 Analytics API 获取账号的真实 token 使用情况

用于监控账号配额和使用量
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BACKEND_BASE_URL = "https://api.stack-ai.com"


@dataclass
class AnalyticsStats:
    """分析统计数据"""
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0.0
    average_latency: float = 0.0
    total_tokens: int = 0
    average_tokens: float = 0.0
    today_tokens: int = 0
    today_runs: int = 0


class AnalyticsService:
    """
    Analytics 服务
    
    通过 Private API Key 调用后端 Analytics API 获取账号使用情况
    """
    
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
    
    async def get_flow_analytics(
        self,
        org_id: str,
        flow_id: str,
        private_api_key: str,
        page: int = 0,
        page_size: int = 100,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取工作流的运行分析数据
        
        Args:
            org_id: 组织 ID
            flow_id: 工作流 ID
            private_api_key: Private API Key
            page: 页码
            page_size: 每页数量
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            运行记录列表，失败返回 None
        """
        url = f"{BACKEND_BASE_URL}/analytics/org/{org_id}/flows/{flow_id}"
        
        params = {
            "page": page,
            "page_size": page_size
        }
        
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {private_api_key}",
                        "Content-Type": "application/json"
                    },
                    params=params
                )
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(
                        f"Analytics API returned {response.status_code}: {response.text[:200]}"
                    )
                    return None
                    
        except httpx.TimeoutException:
            logger.error(f"Analytics API timeout for org={org_id}, flow={flow_id}")
            return None
        except Exception as e:
            logger.error(f"Analytics API error: {e}")
            return None
    
    async def get_today_stats(
        self,
        org_id: str,
        flow_id: str,
        private_api_key: str
    ) -> Optional[AnalyticsStats]:
        """
        获取今日统计数据
        
        Args:
            org_id: 组织 ID
            flow_id: 工作流 ID
            private_api_key: Private API Key
            
        Returns:
            今日统计数据
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        
        # 不传日期参数，获取最近的记录，然后在本地过滤今日数据
        all_runs = await self._get_all_runs(
            org_id=org_id,
            flow_id=flow_id,
            private_api_key=private_api_key
        )
        
        if all_runs is None:
            return None
        
        # 过滤今日的记录 (使用 date 字段，格式可能是 ISO 日期时间)
        today_runs = []
        for r in all_runs:
            run_date = r.get("date", "")
            if run_date:
                # date 字段可能是 ISO 格式，提取日期部分
                run_date_str = run_date[:10] if len(run_date) >= 10 else run_date
                if run_date_str == today:
                    today_runs.append(r)
        
        return self._calculate_stats(today_runs, today_only=True)
    
    async def get_recent_stats(
        self,
        org_id: str,
        flow_id: str,
        private_api_key: str,
        days: int = 7
    ) -> Optional[AnalyticsStats]:
        """
        获取最近 N 天的统计数据
        
        Args:
            org_id: 组织 ID
            flow_id: 工作流 ID
            private_api_key: Private API Key
            days: 天数
            
        Returns:
            统计数据
        """
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        today = end_date.strftime("%Y-%m-%d")
        start_date_str = start_date.strftime("%Y-%m-%d")
        
        # 不传日期参数，获取最近的记录，然后在本地过滤
        all_runs = await self._get_all_runs(
            org_id=org_id,
            flow_id=flow_id,
            private_api_key=private_api_key
        )
        
        if all_runs is None:
            return None
        
        # 过滤指定日期范围内的记录 (使用 date 字段)
        filtered_runs = []
        for r in all_runs:
            run_date = r.get("date", "")
            if run_date:
                run_date_str = run_date[:10] if len(run_date) >= 10 else run_date
                if run_date_str >= start_date_str:
                    filtered_runs.append(r)
        
        return self._calculate_stats(filtered_runs, today_str=today)
    
    async def _get_all_runs(
        self,
        org_id: str,
        flow_id: str,
        private_api_key: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        max_pages: int = 10
    ) -> Optional[List[Dict[str, Any]]]:
        """
        分页获取所有运行记录
        
        Args:
            org_id: 组织 ID
            flow_id: 工作流 ID
            private_api_key: Private API Key
            start_date: 开始日期
            end_date: 结束日期
            max_pages: 最大页数（防止无限循环）
            
        Returns:
            所有运行记录列表
        """
        all_runs = []
        page = 0
        page_size = 200  # StackAI API 最大支持 200
        
        while page < max_pages:
            runs = await self.get_flow_analytics(
                org_id=org_id,
                flow_id=flow_id,
                private_api_key=private_api_key,
                page=page,
                page_size=page_size,
                start_date=start_date,
                end_date=end_date
            )
            
            if runs is None:
                # 第一页就失败，返回 None
                if page == 0:
                    return None
                # 后续页失败，返回已获取的数据
                break
            
            all_runs.extend(runs)
            
            # 如果返回的记录数少于 page_size，说明已经是最后一页
            if len(runs) < page_size:
                break
            
            page += 1
        
        return all_runs

    def _calculate_stats(
        self, 
        runs: List[Dict[str, Any]], 
        today_only: bool = False,
        today_str: Optional[str] = None
    ) -> AnalyticsStats:
        """
        计算统计数据
        
        Args:
            runs: 运行记录列表
            today_only: 是否只计算今日数据
            today_str: 今日日期字符串
            
        Returns:
            统计数据
        """
        if not runs:
            return AnalyticsStats()
        
        if today_str is None:
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
        
        total_runs = len(runs)
        successful_runs = sum(1 for r in runs if r.get("is_flow_successful") is True)
        failed_runs = total_runs - successful_runs
        success_rate = (successful_runs / total_runs * 100) if total_runs > 0 else 0
        
        latencies = [r.get("latency") for r in runs if r.get("latency") is not None]
        average_latency = sum(latencies) / len(latencies) if latencies else 0
        
        tokens = [r.get("total_tokens") or 0 for r in runs]
        total_tokens = sum(tokens)
        average_tokens = total_tokens / len(tokens) if tokens else 0
        
        # 计算今日数据 (使用 date 字段)
        today_tokens = 0
        today_runs = 0
        for r in runs:
            run_date = r.get("date", "")
            if run_date:
                run_date_str = run_date[:10] if len(run_date) >= 10 else run_date
                if run_date_str == today_str:
                    today_runs += 1
                    today_tokens += r.get("total_tokens") or 0
        
        return AnalyticsStats(
            total_runs=total_runs,
            successful_runs=successful_runs,
            failed_runs=failed_runs,
            success_rate=round(success_rate, 2),
            average_latency=round(average_latency, 2),
            total_tokens=total_tokens,
            average_tokens=round(average_tokens, 2),
            today_tokens=today_tokens,
            today_runs=today_runs
        )
    
    async def verify_private_api_key(
        self,
        org_id: str,
        flow_id: str,
        private_api_key: str
    ) -> bool:
        """
        验证 Private API Key 是否有效
        
        Args:
            org_id: 组织 ID
            flow_id: 工作流 ID
            private_api_key: Private API Key
            
        Returns:
            是否有效
        """
        result = await self.get_flow_analytics(
            org_id=org_id,
            flow_id=flow_id,
            private_api_key=private_api_key,
            page=0,
            page_size=1
        )
        return result is not None


# 全局实例
_analytics_service: Optional[AnalyticsService] = None


def get_analytics_service() -> AnalyticsService:
    """获取全局 Analytics 服务实例"""
    global _analytics_service
    if _analytics_service is None:
        _analytics_service = AnalyticsService()
    return _analytics_service


# 兼容旧名称
StackAIAnalyticsService = AnalyticsService
