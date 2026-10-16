"""`src.models` package: the former single module, split by subject; every public name re-exported."""

from src.models.base import (
    reward_to_risk,
    _normalize_symbol,
    _normalize_enum_case_fields,
    ANALYSIS_DROP_KIND,
    DROP_CODE_MALFORMED_ROW,
    DROP_CODE_SCHEMA_INVALID,
    DROP_CODE_UNSPECIFIED,
    ANALYSIS_DROP_CODES,
    AnalysisParseTelemetry,
    parse_telemetry,
    SOFT_EXIT_UNKNOWN,
    _SOFT_EXIT_FIELDS,
    SOFT_EXIT_MISSING_AFTER_RETRY,
    SOFT_EXIT_HEAL_EVENT_REASON,
    ACTIONABLE_TECH_RATINGS,
    stated_soft_exit,
    missing_stated_falsifier,
    open_target_missing_falsifier,
    _NULL_MUST_FAIL,
    _NULL_TOLERANT_FIELDS_CACHE,
    _NULL_TOLERANT_CACHE_LOCK,
    _null_droppable_fields,
    _UNSOURCED_TOKEN_RE,
    _LIST_TYPED_FIELDS_CACHE,
    _LIST_TYPED_FIELDS_CACHE_LOCK,
    _list_typed_fields,
    LLMOutputModel,
    _ALLOWED_SECTORS,
    _SECTOR_ALIASES,
    SECTOR_STANCE_TO_DIRECTION,
    SECTOR_DIRECTIONS,
    normalize_sector_stance,
)
from src.models.analysis import (
    Nomination,
    _sanitize_nominations_field,
    OHLCV,
    TechnicalIndicators,
    VerdictEvidence,
    AnalystVerdict,
    RATING_SCALE,
    rating_rung_strength,
    NO_STATED_STRENGTH,
    RATING_DIRECTION,
    TechReasoningChain,
    TechAnalystAnswerItem,
    TechAnalystAnswer,
    TechAnalysisResult,
)
from src.models.decisions import (
    TradeDecision,
    ReasoningChain,
    ExitReviewChain,
    AnalystProvenance,
)
from src.models.smart_money import (
    InsiderPurchaseCluster,
    SmartMoneyObservation,
    _SMART_MONEY_ROLE_CONVICTION,
    _purchase_cluster_lift,
    SmartMoneyFinding,
    SmartMoneyNoRead,
    SmartMoneySynthesis,
)
from src.models.risk_narrative_claims import (
    _RISK_PCT_CLAIM_PATTERN,
    _explicit_risk_pct_claim_texts,
    _explicit_risk_pct_claims,
    risk_pct_half_ulp,
)
from src.models.portfolio import (
    TargetPosition,
    CANDIDATE_REJECTION_CODES,
    CandidateRejection,
    PortfolioDecision,
    RiskModification,
    _normalize_rejected_symbols_field,
    SymbolRejection,
    EntrySymbolRejection,
)
from src.models.risk_verdicts import (
    RiskReasonCategory,
    _PerSymbolRejections,
    RiskReasoningChain,
    ExitRiskReasoningChain,
    ExitRiskVerdict,
    RiskVerdict,
)
from src.models.macro import (
    MacroObservation,
    MacroSectorGuidance,
    MacroPositionGuidance,
    MacroReasoningChain,
    MacroAnalysis,
    MacroNarrative,
)
from src.models.news import (
    StateChange,
    StockNewsItem,
    _NEWS_CONVICTION_RANK,
    _MAX_NEWS_EVIDENCE_ITEMS,
    news_verdict_for_symbol,
    NewsIntelligenceReport,
)
from src.models.positions import (
    Position,
    PositionAction,
    TargetRevisionFlag,
    PositionReasoningChain,
    PositionReview,
)
from src.models.earnings import (
    EarningsSegment,
    EarningsRevenue,
    EarningsProfitability,
    EarningsCashFlow,
    EarningsBalanceSheet,
    EarningsStrategicDirection,
    EarningsRiskFlags,
    EarningsReasoningChain,
    EarningsInvestmentImplications,
    EarningsAnalysis,
)
from src.models.evening import (
    EveningReasoningChain,
    ThesisTrajectory,
    SellGrade,
    BuyLossRootCause,
    BuyGrade,
    MissedOpportunitySnapshot,
    MissedOpportunity,
    EveningReport,
    AgentLog,
)
from src.models.meta import (
    MetaReflectionAgentName,
    MetaReasoningChain,
    ThemeCoverage,
    MetaLossRootCause,
    LossPattern,
    LossPatternReport,
    PromptLearning,
    QuarterlyMetaReflection,
)

# Board item 78: a later blank write must not erase a stated falsifier.
from src.models.soft_exit_guard import install as _install_soft_exit_guard

for _guarded in (TargetPosition, TradeDecision):
    _install_soft_exit_guard(_guarded)
