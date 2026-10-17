import { useRef } from 'react';
import { DataChart } from './DataChart';

const KNOWN_METRICS = [
  'capacity_mw',
  'budget_allocated',
  'budget_used',
  'budget_remaining',
  'revenue',
  'completion_percentage',
  'delay_days',
  'operating_cost',
  'marketing_spend',
  'tax_liability',
  'asset_value',
  'customer_count',
  'profit',
  'expenses',
  'headcount',
  'salary'
];

function extractMetrics(sql: string, results: any[]): string[] {
  if (!results || results.length === 0) return ['revenue'];

  const sqlLower = (sql || '').toLowerCase();
  const foundMetrics: string[] = [];

  KNOWN_METRICS.forEach(m => {
    const regex = new RegExp(`\\b${m}\\b`, 'i');
    if (regex.test(sqlLower)) {
      foundMetrics.push(m);
    }
  });

  if (foundMetrics.length > 0) {
    return foundMetrics;
  }

  const keys = Object.keys(results[0]);
  const matchedKeys = keys.filter(k => KNOWN_METRICS.includes(k.toLowerCase()));
  if (matchedKeys.length > 0) {
    return matchedKeys;
  }

  const partialMatchedKeys: string[] = [];
  keys.forEach(k => {
    KNOWN_METRICS.forEach(m => {
      if (k.toLowerCase().includes(m)) {
        partialMatchedKeys.push(m);
      }
    });
  });
  if (partialMatchedKeys.length > 0) {
    return Array.from(new Set(partialMatchedKeys));
  }

  const dimensionKeys = [
    'record_date', 'year', 'month', 'plant', 'department', 'region', 
    'comparison_group', 'plant_name', 'id'
  ];
  const numericKeys = keys.filter(k => {
    const val = results[0][k];
    const isNum = typeof val === 'number';
    const isYear = /^(19|20)\d{2}$/.test(k) || (!isNaN(Number(k)) && Number(k) > 1900 && Number(k) < 2100);
    return isNum && !dimensionKeys.includes(k.toLowerCase()) && !isYear;
  });

  if (numericKeys.length > 0) {
    return numericKeys;
  }

  return ['revenue'];
}

interface ResultsDisplayProps {
  data: any;
  devMode: boolean;
  onFollowUpClick: (query: string) => void;
  darkMode?: boolean;
  isLoading?: boolean;
  activeTypingMetrics?: string[];
  relatedMetrics?: string[];
  metadataMetrics?: string[];
  detectedState?: string | null;
  detectedYear?: string | null;
  cacheCatalog?: any[];
  lineageNodes?: any[];
  queryLabel?: string;
}

const getMetricDisplayName = (key: string) => {
  if (key === 'completion_percentage') return 'Completion %';
  return key.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
};

const getAggregationLabel = (agg?: string): string => {
  if (!agg) return '';
  switch (agg.toUpperCase()) {
    case 'SUM':   return 'Total';
    case 'AVG':   return 'Average';
    case 'MIN':   return 'Min';
    case 'MAX':   return 'Max';
    case 'COUNT': return 'Count';
    default:      return agg;
  }
};

export function ResultsDisplay({ 
  data, 
  devMode, 
  onFollowUpClick, 
  darkMode, 
  isLoading = false,
  activeTypingMetrics = [],
  detectedState = null,
  detectedYear = null,
  cacheCatalog = [],
  lineageNodes = [],
  queryLabel = '',
}: ResultsDisplayProps) {
  const summaryRef = useRef<HTMLDivElement>(null);

  // If loading, show ghost metric cards only for metrics the user is asking about
  if (isLoading) {
    return (
      <div className="p-8">
        <p className="text-sm text-stone-500 dark:text-slate-400">Running the query.</p>
      </div>
    );
  }

  if (data?.status === "clarification_required") {
    const suggestions = data.suggestions || [];
    return (
      <div className="p-8">
        <div className="max-w-md bg-white dark:bg-slate-900 border border-stone-300 dark:border-slate-800 p-5">
          <h3 className="text-sm text-stone-900 dark:text-slate-100 mb-2">Did you mean</h3>
          <p className="text-sm text-stone-500 dark:text-slate-400 mb-4">
            {data.message || "That name was not found. Try one of these."}
          </p>
          <div className="flex flex-col">
            {suggestions.map((sug: string, idx: number) => (
              <button
                key={`${sug}-${idx}`}
                type="button"
                onClick={() => onFollowUpClick(sug)}
                className="py-1.5 text-left text-sm text-stone-800 dark:text-slate-200 hover:underline"
              >
                {sug}
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (!data || !data.results || data.results.length === 0) {
    return (
      <div className="p-6">
        <p className="text-sm text-slate-500">
          {data?.status === 'success' || data?.status === 'error'
            ? (data.message || 'No rows matched this question.')
            : 'Ask a question in the sidebar, then press Enter.'}
        </p>
      </div>
    );
  }

  const { results, sql_query, unit, plants_queried, insights, metadata, kpis, aggregations, semantic_report } = data;

  const getCurrencyPrefix = () => {
    if (unit && unit.toLowerCase() === 'usd') {
      return '$';
    }
    return '₹';
  };

  const formatKPI = (val: any) => {
    if (val == null) return "0";
    const prefix = getCurrencyPrefix();
    if (val >= 1000000) return `${prefix}${(val / 1000000).toFixed(1)}M`;
    if (val >= 1000) return `${prefix}${(val / 1000).toFixed(1)}K`;
    return prefix + val.toLocaleString();
  };

  const formatHeadcount = (val: any) => {
    if (val == null) return "0";
    return val.toLocaleString();
  };

  const formatMetricValue = (val: any, metricName: string) => {
    if (val == null) return "0";
    const k = metricName.toLowerCase().replace(/_/g, ' ');
    if (k.includes('capacity')) {
      return `${val.toLocaleString()} MW`;
    }
    if (k.includes('percent') || k.includes('pct')) {
      return `${val.toLocaleString()}%`;
    }
    if (k.includes('delay') || k.includes('days')) {
      return `${val.toLocaleString()} Days`;
    }
    if (['headcount', 'customer_count', 'customers', 'employees', 'people'].some(keyword => k.includes(keyword))) {
      return val.toLocaleString();
    }
    const prefix = getCurrencyPrefix();
    if (val >= 1000000) return `${prefix}${(val / 1000000).toFixed(1)}M`;
    if (val >= 1000) return `${prefix}${(val / 1000).toFixed(1)}K`;
    return prefix + val.toLocaleString();
  };

  const generateExecutiveSummary = () => {
    if (!results || results.length === 0) return { takeaway: 'No data available for this result.', detail: '' };
    
    const keys = Object.keys(results[0]);
    const detectedMetrics = extractMetrics(sql_query, results);
    const primaryMetric = detectedMetrics[0] ? detectedMetrics[0].replace(/_/g, ' ') : "data";
    
    const numPlants = plants_queried || 0;
    
    let timeSpan = "the selected timeframe";
    const recordDateKey = keys.find(k => k.includes('date') || k === 'year');
    if (recordDateKey) {
      const years = Array.from(new Set(results.map((r: any) => String(r[recordDateKey]).substring(0, 4))));
      if (years.length > 0) {
        timeSpan = years.join(', ');
      }
    }
    
    let peakInsight = "";
    const dimensionKeys = ['record_date', 'year', 'month', 'plant', 'department', 'region', 'comparison_group', 'plant_name', 'id'];
    const valueKeys = keys.filter(k => {
      const isNum = typeof results[0][k] === 'number';
      return isNum && !dimensionKeys.includes(k.toLowerCase());
    });

    if (valueKeys.length > 0) {
      const labelKey = keys.find(k => typeof results[0][k] === 'string') || 'record_date';
      let maxVal = -Infinity;
      let maxEntity = "";
      let maxKey = "";
      
      results.forEach((row: any) => {
        const entity = row[labelKey] ? row[labelKey].toString() : "";
        valueKeys.forEach(vk => {
          const val = row[vk];
          if (typeof val === 'number' && val > maxVal) {
            maxVal = val;
            maxEntity = entity;
            maxKey = vk;
          }
        });
      });
      
      if (maxVal !== -Infinity) {
        const valFormatted = formatMetricValue(maxVal, primaryMetric);
        const entityPart = maxEntity ? ` in ${maxEntity.replace(/_/g, ' ')}` : "";
        const contextPart = (/^(19|20)\d{2}$/.test(maxKey) || valueKeys.length > 1) ? ` (${maxKey.replace(/_/g, ' ')})` : "";
        peakInsight = `Highest ${primaryMetric}${contextPart} of ${valFormatted}${entityPart}.`;
      }
    }
    
    const metricLabel = primaryMetric.charAt(0).toUpperCase() + primaryMetric.slice(1);
    const takeaway = peakInsight || metricLabel;
    const detailParts = [];
    if (timeSpan !== 'the selected timeframe') detailParts.push(`${metricLabel} from ${timeSpan}`);
    if (numPlants > 0) detailParts.push(`across ${numPlants} plants`);
    return { takeaway, detail: detailParts.join(', ') };
  };

  const getFollowUpQuestions = () => {
    const detectedMetrics = extractMetrics(sql_query, results);
    const m = detectedMetrics[0] || "revenue";
    const cleanM = m.replace(/_/g, ' ');
    const capM = cleanM.charAt(0).toUpperCase() + cleanM.slice(1);
    
    // Extract active filters from SQL query or results
    const sqlLower = (sql_query || '').toLowerCase();
    const depts = ["solar", "wind", "hybrid", "hybrid-solar", "hybrid-wind", "sales", "digital", "marketing", "hr", "engineering", "finance", "support", "operations"];
    const regions = ["gujarat", "karnataka", "maharashtra", "rajasthan", "tamil nadu", "north", "south", "east", "west", "central"];
    const plants = ["diablo_canyon", "three_mile_island", "palo_verde", "grand_gulf", "vogtle", "hinkley_point", "kashiwazaki", "darlington"];
    
    const activeDept = depts.find(d => sqlLower.includes(`'${d}'`) || sqlLower.includes(`'${d.toLowerCase()}'`));
    const activeRegion = regions.find(r => sqlLower.includes(`'${r}'`) || sqlLower.includes(`'${r.toLowerCase()}'`));
    
    let activePlant = plants.find(p => sqlLower.includes(p));
    if (!activePlant && results.length > 0) {
      for (const row of results) {
        if (row.plant) {
          activePlant = plants.find(p => p === row.plant.toLowerCase() || p.replace('_', ' ') === row.plant.toLowerCase());
          if (activePlant) break;
        }
      }
    }
    
    const yearMatch = sqlLower.match(/\b(202[0-7])\b/);
    const activeYear = yearMatch ? yearMatch[1] : null;
    const timeLabel = activeYear ? `in ${activeYear}` : "";
    
    // Format helpers
    const formatDept = (d: string) => {
      if (d === "hr" || d === "digital") return d.toUpperCase();
      return d.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join('-');
    };
    const formatRegion = (r: string) => r.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    const formatPlant = (p: string) => p.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    
    const deptLabel = activeDept ? formatDept(activeDept) : "";
    const regionLabel = activeRegion ? formatRegion(activeRegion) : "";
    const plantLabel = activePlant ? formatPlant(activePlant) : "";
    
    const questions = [];
    
    // Detect ID / Contract / Asset / Warehouse query
    const hasContractId = results[0]?.contract_ref || sqlLower.includes('contract_ref');
    const hasAssetCode = results[0]?.asset_code || sqlLower.includes('asset_code');
    const hasWarehouseId = results[0]?.warehouse_id || sqlLower.includes('warehouse_id');
    const hasProjectId = results[0]?.project_id || sqlLower.includes('project_id');
    
    if (hasContractId) {
      const contractId = results[0]?.contract_ref || "REF-001";
      const clientName = results[0]?.client_name || "Acme Corp";
      questions.push(`What is the delivery status of contract ${contractId}?`);
      questions.push(`Show project cost and completion date for contract ${contractId}`);
      questions.push(`Total project cost for ${clientName}`);
      questions.push(`Show all contracts with In Progress delivery status`);
      questions.push(`Show all contracts with Pending delivery status`);
    } else if (hasAssetCode) {
      const assetCode = results[0]?.asset_code || "AST-001";
      const plantOrFacility = results[0]?.facility_name || results[0]?.plant || "Solar Facility Alpha";
      questions.push(`What is the vendor and commission date of asset ${assetCode}?`);
      questions.push(`Show energy output by vendor for ${plantOrFacility}`);
      questions.push(`Show all assets for vendor SunPower`);
      questions.push(`Top 3 plants by energy output`);
      questions.push(`Show all assets commissioned after 2023`);
    } else if (hasWarehouseId) {
      const warehouseId = results[0]?.warehouse_id || "WH-001";
      questions.push(`What is the inventory turnover and stock value for warehouse ${warehouseId}?`);
      questions.push(`Show stock value by supplier for ${warehouseId}`);
      questions.push(`Show all warehouses with inventory turnover above 4.0`);
      questions.push(`Top 3 warehouses by stock value`);
      questions.push(`Total stock value across all warehouses`);
    } else if (hasProjectId) {
      const pid = results[0]?.project_id || "PRJ-DAR-000001";
      questions.push(`Show all details of project ${pid}`);
      questions.push(`What is the completion percentage of project ${pid}?`);
      questions.push(`What is the delay days for project ${pid}?`);
      questions.push(`Show contractor name and material status for project ${pid}`);
      questions.push(`Compare project ${pid} with other projects in the same plant`);
    } else {
      const isTrend = sqlLower.includes('strftime') || metadata?.intent === 'trend' || sqlLower.includes('group by strftime');
      const isBreakdown = sqlLower.includes('group by') && !isTrend;
      
      if (isTrend) {
        questions.push(`What was the ${cleanM} breakdown by plant ${timeLabel}`.trim());
        questions.push(`What was the ${cleanM} breakdown by state ${timeLabel}`.trim());
        questions.push(`What was the ${cleanM} breakdown by project type ${timeLabel}`.trim());
        questions.push(`Compare ${cleanM} and Budget Used ${timeLabel}`.trim());
        questions.push(`Show ${cleanM} trend for last 3 years`);
      } else if (isBreakdown) {
        const isGroupByPlant = sqlLower.includes('group by plant') || sqlLower.includes('group by comparison_group');
        const isGroupByState = sqlLower.includes('group by state') || sqlLower.includes('group by location');
        
        if (isGroupByPlant) {
          questions.push(`Show ${cleanM} trend by plant ${timeLabel}`.trim());
          questions.push(`Compare ${cleanM} of top performing plants`);
          questions.push(`What was the capacity MW breakdown by plant ${timeLabel}`.trim());
          questions.push(`What was the budget allocated by plant ${timeLabel}`.trim());
          questions.push(`Show completion percentage by plant ${timeLabel}`.trim());
        } else if (isGroupByState) {
          questions.push(`Show ${cleanM} trend by state ${timeLabel}`.trim());
          questions.push(`Compare ${cleanM} in Gujarat and Rajasthan ${timeLabel}`.trim());
          questions.push(`What was the budget allocated by state ${timeLabel}`.trim());
          questions.push(`Show delay days breakdown by state ${timeLabel}`.trim());
          questions.push(`Top performing plants in Gujarat`);
        } else {
          questions.push(`Show ${cleanM} trend by project type ${timeLabel}`.trim());
          questions.push(`Compare ${cleanM} of Solar and Wind ${timeLabel}`.trim());
          questions.push(`What was the completion percentage by project type ${timeLabel}`.trim());
          questions.push(`What was the delay days breakdown by project type ${timeLabel}`.trim());
          questions.push(`Compare budget allocated by project type ${timeLabel}`.trim());
        }
      } else {
        if (activeDept && activeRegion) {
          questions.push(`Compare ${deptLabel} ${cleanM} in ${regionLabel} and South`);
          questions.push(`Show ${deptLabel} ${cleanM} trend in ${regionLabel} over time`);
          questions.push(`Compare ${deptLabel} and Digital ${cleanM} in ${regionLabel}`);
          questions.push(`Show ${deptLabel} ${cleanM} breakdown by plant in ${regionLabel}`);
          questions.push(`Top performing plants for ${deptLabel} in ${regionLabel}`);
        } else if (activePlant) {
          questions.push(`Show ${cleanM} trend for ${plantLabel}`);
          questions.push(`What was the ${cleanM} breakdown by project type for ${plantLabel}?`);
          questions.push(`Compare ${plantLabel} and Darlington ${cleanM}`);
          questions.push(`Show all metrics for ${plantLabel}`);
          questions.push(`What was the completion percentage for ${plantLabel}?`);
        } else if (activeRegion) {
          questions.push(`Show ${cleanM} trend in ${regionLabel}`);
          questions.push(`What was the ${cleanM} breakdown by project type in ${regionLabel}?`);
          questions.push(`Compare ${cleanM} in ${regionLabel} and Rajasthan`);
          questions.push(`Show ${cleanM} breakdown by plant in ${regionLabel}`);
          questions.push(`Top 3 performing plants in ${regionLabel}`);
        } else if (activeDept) {
          questions.push(`Show ${deptLabel} ${cleanM} trend over time`);
          questions.push(`What was the ${deptLabel} ${cleanM} breakdown by state?`);
          questions.push(`What was the ${deptLabel} ${cleanM} breakdown by plant?`);
          questions.push(`Compare ${deptLabel} and Wind ${cleanM}`);
          questions.push(`Top 3 plants by ${deptLabel} ${cleanM}`);
        } else {
          questions.push(`Show ${cleanM} trend over time`);
          questions.push(`What was the ${cleanM} breakdown by plant ${timeLabel}`.trim());
          questions.push(`What was the ${cleanM} breakdown by state ${timeLabel}`.trim());
          questions.push(`What was the ${cleanM} breakdown by project type ${timeLabel}`.trim());
          questions.push(`Compare ${cleanM} in 2025 and 2026`);
        }
      }
    }
    
    return questions.slice(0, 5);
  };

  const formatKPIValue = (val: any, key: string) => {
    if (val == null) return "0";
    const k = key.toLowerCase();
    if (k.includes('capacity')) {
      return `${val.toLocaleString()} MW`;
    }
    if (k.includes('percent') || k.includes('pct')) {
      return `${val.toLocaleString()}%`;
    }
    if (k.includes('delay') || k.includes('days')) {
      return `${val.toLocaleString()} Days`;
    }
    if (k.includes('headcount') || k.includes('count')) {
      return val.toLocaleString();
    }
    // Currency formatting
    const prefix = getCurrencyPrefix();
    if (val >= 1000000) return `${prefix}${(val / 1000000).toFixed(1)}M`;
    if (val >= 1000) return `${prefix}${(val / 1000).toFixed(1)}K`;
    return prefix + val.toLocaleString();
  };

  // Build a fixed 4-slot grid for completed results:
  // Real KPI values first (prioritise detected), fill remaining slots with ghost cards
  const kpiEntries = kpis ? Object.entries(kpis) : [];
  // Put detected metrics first
  const sortedKpiEntries = [
    ...kpiEntries.filter(([k]) => activeTypingMetrics.includes(k)),
    ...kpiEntries.filter(([k]) => !activeTypingMetrics.includes(k))
  ].slice(0, 4);

  return (
    <div className="p-6 space-y-5">
      {sortedKpiEntries.length > 0 && (
        <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
          {sortedKpiEntries.map(([key, val], idx) => {
            const name = getMetricDisplayName(key);
            const formattedVal = formatKPIValue(val, key);
            const aggLabel = aggregations ? getAggregationLabel(aggregations[key]) : '';
            return (
              <div key={`${key}-${idx}`} className="work-glass flex min-h-[108px] flex-col justify-between rounded-2xl border border-slate-200/80 bg-white px-5 py-4 dark:border-emerald-400/15 dark:bg-slate-900">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs text-slate-500 dark:text-slate-400">{name}</p>
                  {aggLabel && <span className="text-[11px] text-emerald-700 dark:text-emerald-300/80">{aggLabel}</span>}
                </div>
                <p className={`mt-3 font-medium tracking-tight text-slate-900 dark:text-white ${String(formattedVal).length > 16 ? 'text-lg leading-snug' : 'text-2xl'}`}>{formattedVal}</p>
              </div>
            );
          })}
        </div>
      )}

      <div className="work-glass rounded-2xl border border-slate-200/80 bg-white px-5 py-4 dark:border-white/10 dark:bg-slate-900">
        <div className="w-full">
          <DataChart
            results={results}
            unit={unit}
            darkMode={darkMode}
            title={queryLabel.trim() || (extractMetrics(sql_query, results)[0] || 'Result').replace(/_/g, ' ')}
          />
        </div>
        {(() => {
          const insight = generateExecutiveSummary();
          return (
            <div className="mt-4 border-t border-slate-100 pt-4 dark:border-white/10">
              <p className="text-xs text-emerald-700 dark:text-emerald-300/80">Key insight</p>
              <p className="mt-1.5 text-base font-medium leading-snug text-slate-900 dark:text-white">{insight.takeaway}</p>
              <p className="mt-1 text-sm leading-relaxed text-slate-500 dark:text-slate-400">{insight.detail}</p>
            </div>
          );
        })()}
      </div>

      <div ref={summaryRef} className="rounded-2xl bg-white dark:bg-slate-900 px-5 py-4">
        <h3 className="text-xs text-slate-500 mb-3">Table</h3>
          <div className="overflow-auto max-h-72">
            <table className="w-full text-sm text-left">
              <thead className="bg-stone-50 dark:bg-slate-800 sticky top-0 border-b border-stone-200 dark:border-slate-800">
                <tr>
                  {Object.keys(results[0] || {}).map((key, idx) => {
                    const aggLabel = aggregations ? getAggregationLabel(aggregations[key]) : '';
                    return (
                      <th key={`${key}-${idx}`} className="py-2 px-3 text-xs font-medium text-stone-500 dark:text-slate-400 whitespace-nowrap">
                        <span className="capitalize">{key.replace(/_/g, ' ')}</span>
                        {aggLabel && (
                          <span className="ml-1 text-stone-400">{aggLabel}</span>
                        )}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-slate-800 bg-white dark:bg-slate-900">
                {results.map((row: any, idx: number) => (
                  <tr key={idx} className="hover:bg-gray-50/40 dark:hover:bg-slate-850/30 transition-colors">
                    {Object.values(row).map((value: any, cellIdx: number) => (
                      <td key={cellIdx} className="py-3 px-4 text-gray-900 dark:text-slate-200 font-medium">
                        {typeof value === 'number' ? value.toLocaleString() : value}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

      {/* 6. Developer Mode Panel */}
      {devMode && (
        <div className="space-y-6 pt-6 border-t border-gray-150 dark:border-slate-800 mt-8">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-bold text-gray-805 dark:text-slate-205 uppercase tracking-wider flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-blue-500 animate-pulse"></span>
              IQP Observability & Developer Diagnostics
            </h3>
            <span className="px-2.5 py-1 bg-blue-50 dark:bg-blue-950/30 text-blue-700 dark:text-blue-400 text-[10px] font-bold rounded-full border border-blue-100/30 dark:border-blue-900/30">
              Dev Mode Active
            </span>
          </div>

          {/* Top Summary Cards */}
          <div className="grid grid-cols-4 gap-4">
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800/80 shadow-sm rounded-2xl p-4 flex flex-col justify-center">
              <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-1">Execution Route</p>
              <p className="text-sm font-bold text-gray-800 dark:text-slate-200">
                {metadata?.iqp?.execution_details?.execution_route || (metadata?.parsed_deterministically ? "⚡ Deterministic SQLite" : "🤖 Ollama LLM")}
              </p>
            </div>
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800/80 shadow-sm rounded-2xl p-4 flex flex-col justify-center">
              <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-1">Cache Reuse Status</p>
              <div className="text-sm font-bold text-gray-800 dark:text-slate-200 flex items-center gap-1.5">
                {metadata?.iqp ? (
                  metadata.iqp.cache_hit ? (
                    <>
                      <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
                      <span>Hit ({metadata.iqp.cache_type})</span>
                    </>
                  ) : (
                    <>
                      <span className="w-2 h-2 rounded-full bg-amber-500"></span>
                      <span>Miss (Cold Fetch)</span>
                    </>
                  )
                ) : (
                  metadata?.cache_hit ? "✅ Result Cache" : metadata?.used_semantic_cache ? "🧠 Semantic Cache" : "❌ Cold DB Fetch"
                )}
              </div>
            </div>
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800/80 shadow-sm rounded-2xl p-4 flex flex-col justify-center">
              <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-1">Freshness Status</p>
              <div className="text-sm font-bold text-gray-800 dark:text-slate-200 flex items-center gap-1.5">
                {metadata?.iqp ? (
                  metadata.iqp.freshness === "VALID" ? (
                    <>
                      <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
                      <span className="text-emerald-600 dark:text-emerald-400">VALID</span>
                    </>
                  ) : (
                    <>
                      <span className="w-2 h-2 rounded-full bg-red-500"></span>
                      <span className="text-red-600 dark:text-red-400">INVALID</span>
                    </>
                  )
                ) : (
                  "N/A"
                )}
              </div>
            </div>
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800/80 shadow-sm rounded-2xl p-4 flex flex-col justify-center">
              <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-1">Latency / Scan Count</p>
              <p className="text-sm font-bold text-gray-800 dark:text-slate-200">
                {Math.round(metadata?.iqp?.execution_details?.execution_time_ms || metadata?.backend_ms || 0)}ms / {metadata?.iqp?.execution_details?.rows_scanned?.toLocaleString() || "—"} rows
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* 1. IQP Diagnostics Panel */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">1. IQP Diagnostics Panel</h4>
              {metadata?.iqp ? (
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Query Node ID</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.node_id != null ? `#${metadata.iqp.node_id}` : "N/A"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Parent Node ID</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.parent_id != null ? `#${metadata.iqp.parent_id}` : "None"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Lineage Depth</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.depth}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Blueprint Hash</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200 truncate select-all" title={metadata.iqp.blueprint_hash}>{metadata.iqp.blueprint_hash || "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 col-span-2">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Session ID</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200 select-all">{metadata.iqp.session_id || "None"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 col-span-2">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Query Timestamp</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">
                      {metadata.iqp.timestamp ? new Date(metadata.iqp.timestamp * 1000).toLocaleString() : "N/A"}
                    </span>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-400 italic">No IQP diagnostics recorded for this query.</p>
              )}
            </div>

            {/* 3. Routing Decision Visibility */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">3. Routing Decision Visibility</h4>
              {metadata?.iqp ? (
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Route Selected</span>
                    <span className="font-semibold text-blue-600 dark:text-blue-400">
                      {metadata.iqp.route === "duckdb_local" ? "DuckDB Local" : metadata.iqp.route === "result_cache" ? "Result Cache" : "Federated SQLite"}
                    </span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Reuse Score</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.reuse_score != null ? metadata.iqp.reuse_score.toFixed(2) : "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Ancestor Used</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.ancestor_node != null ? `Node ${metadata.iqp.ancestor_node}` : "None"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Cache Hit / Type</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.cache_hit ? "TRUE" : "FALSE"} ({metadata.iqp.cache_type})</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Cost Local</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.cost_local_ms != null ? `${Math.round(metadata.iqp.cost_local_ms)} ms` : "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Cost Federated</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.cost_federated_ms != null ? `${Math.round(metadata.iqp.cost_federated_ms)} ms` : "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 col-span-2">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Decision Reason</span>
                    <span className="font-semibold text-gray-805 dark:text-slate-205">{metadata.iqp.decision_reason || "—"}</span>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-400 italic">No routing decisions recorded.</p>
              )}
            </div>

            {/* 2. Delta Engine Visibility */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">2. Delta Engine Visibility</h4>
              {metadata?.iqp ? (
                <div className="flex-1 flex flex-col justify-between">
                  <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-2">Blueprint Delta Actions</p>
                  <div className="bg-gray-50 dark:bg-slate-950 border border-gray-100 dark:border-slate-850 rounded-xl p-3.5 overflow-x-auto max-h-48 font-mono text-[11px] text-gray-800 dark:text-slate-350 flex-1">
                    {metadata.iqp.delta && metadata.iqp.delta.length > 0 ? (
                      <pre>{JSON.stringify(metadata.iqp.delta, null, 2)}</pre>
                    ) : (
                      <span className="text-gray-400 italic">[] (No delta generated)</span>
                    )}
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-400 italic">No delta engine analysis.</p>
              )}
            </div>

            {/* 5. Freshness Diagnostics */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">5. Freshness Diagnostics</h4>
              {metadata?.iqp ? (
                <div className="grid grid-cols-1 gap-3 text-xs flex-1">
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Composite Database Version Hash</span>
                    <span className="font-mono text-gray-850 dark:text-slate-205 select-all block truncate" title={metadata.iqp.composite_version_hash}>{metadata.iqp.composite_version_hash || "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Cached Version Hash</span>
                    <span className="font-mono text-gray-850 dark:text-slate-205 select-all block truncate" title={metadata.iqp.cached_version_hash}>{metadata.iqp.cached_version_hash || "—"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 flex items-center justify-between">
                    <div>
                      <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Freshness Status</span>
                      <span className={`font-bold ${metadata.iqp.freshness === "VALID" ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}`}>
                        {metadata.iqp.freshness || "VALID"}
                      </span>
                    </div>
                    <div className="text-right">
                      <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Reason</span>
                      <span className="font-medium text-gray-700 dark:text-slate-350">{metadata.iqp.freshness_reason || "—"}</span>
                    </div>
                  </div>
                </div>
              ) : (
                <p className="text-xs text-gray-400 italic">No freshness records available.</p>
              )}
            </div>

            {/* 4. Ancestor Climbing Diagnostics */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">4. Ancestor Climbing Diagnostics</h4>
              {metadata?.iqp && metadata.iqp.climbing && metadata.iqp.climbing.ancestor_selected != null ? (
                <div className="grid grid-cols-2 gap-3 text-xs flex-1">
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Immediate Parent</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">Node {metadata.iqp.climbing.immediate_parent}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Parent Cache Available</span>
                    <span className="font-semibold text-gray-850 dark:text-slate-250">{metadata.iqp.climbing.parent_cache_available ? "TRUE" : "FALSE"}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Ancestor Selected</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">Node {metadata.iqp.climbing.ancestor_selected}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Climb Levels</span>
                    <span className="font-mono text-gray-800 dark:text-slate-200">{metadata.iqp.climbing.climb_levels}</span>
                  </div>
                  <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 col-span-2 flex flex-col">
                    <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase mb-1">Cumulative Delta</span>
                    <div className="bg-gray-100 dark:bg-slate-950/40 p-2 rounded-lg font-mono text-[10px] text-gray-700 dark:text-slate-400 max-h-24 overflow-y-auto">
                      {metadata.iqp.climbing.cumulative_delta && metadata.iqp.climbing.cumulative_delta.length > 0 ? (
                        <pre>{JSON.stringify(metadata.iqp.climbing.cumulative_delta, null, 2)}</pre>
                      ) : (
                        "[] (No delta)"
                      )}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="bg-slate-50 dark:bg-slate-950/20 p-4 rounded-xl border border-dashed border-gray-250 dark:border-slate-800 text-center flex flex-col justify-center items-center flex-1 min-h-[160px]">
                  <span className="text-gray-400 dark:text-slate-500 font-bold text-xs uppercase mb-1">Ancestor Climbing</span>
                  <span className="text-sm font-semibold text-gray-500 dark:text-slate-400">Not Used</span>
                </div>
              )}
            </div>

            {/* 8. IQP Lineage Timeline */}
            <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">8. IQP Timeline</h4>
              <div className="flex-1 flex flex-col justify-center border border-dashed border-gray-100 dark:border-slate-850 rounded-xl p-3 bg-slate-50/50 dark:bg-slate-950/25 overflow-y-auto max-h-72">
                {lineageNodes && lineageNodes.length > 0 ? (
                  <div className="flex flex-col items-center space-y-1.5 py-2">
                    {lineageNodes.map((node, index) => {
                      const isCurrent = node.node_id === metadata?.iqp?.node_id;
                      return (
                        <div key={node.node_id} className="flex flex-col items-center w-full max-w-[280px]">
                          {index > 0 && (
                            <div className="text-gray-300 dark:text-slate-700 text-xs font-black select-none my-0.5">↓</div>
                          )}
                          <div className={`w-full px-3 py-2 rounded-xl border text-[11px] font-semibold shadow-sm transition-all text-center ${
                            isCurrent 
                              ? 'bg-blue-50 dark:bg-blue-950/45 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800 scale-102 ring-2 ring-blue-500/10' 
                              : 'bg-white dark:bg-slate-900 text-gray-600 dark:text-slate-400 border-gray-150 dark:border-slate-850 hover:border-gray-300 dark:hover:border-slate-750'
                          }`}>
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-mono text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-slate-800 text-gray-400 dark:text-slate-500">#{node.node_id}</span>
                              <span className="truncate flex-1 text-left" title={node.raw_query}>{node.raw_query}</span>
                              {node.is_subset && <span className="text-[9px] text-emerald-500 font-bold uppercase">Subset</span>}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-gray-400 dark:text-slate-500 italic text-center py-6">No lineage history recorded for this session.</p>
                )}
              </div>
            </div>
          </div>

          {/* 7. Query Execution Details */}
          <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5">
            <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">7. Query Execution Details</h4>
            {metadata?.iqp && metadata.iqp.execution_details ? (
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
                <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                  <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Execution Route</span>
                  <span className="font-semibold text-gray-800 dark:text-slate-200">{metadata.iqp.execution_details.execution_route}</span>
                </div>
                <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                  <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Execution Time</span>
                  <span className="font-mono text-gray-800 dark:text-slate-200 font-bold">{Math.round(metadata.iqp.execution_details.execution_time_ms)} ms</span>
                </div>
                <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                  <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Rows Returned</span>
                  <span className="font-mono text-gray-800 dark:text-slate-200 font-bold">{metadata.iqp.execution_details.rows_returned?.toLocaleString()}</span>
                </div>
                <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850">
                  <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Rows Scanned</span>
                  <span className="font-mono text-gray-800 dark:text-slate-200 font-bold">{metadata.iqp.execution_details.rows_scanned?.toLocaleString()}</span>
                </div>
                <div className="bg-slate-50/50 dark:bg-slate-950/20 p-2.5 rounded-xl border border-gray-100/50 dark:border-slate-850 col-span-2 md:col-span-1">
                  <span className="block text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase">Databases Queried</span>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {metadata.iqp.execution_details.databases_queried && metadata.iqp.execution_details.databases_queried.length > 0 ? (
                      metadata.iqp.execution_details.databases_queried.map((db: string) => (
                        <span key={db} className="px-1.5 py-0.5 rounded bg-gray-150 dark:bg-slate-800 text-[9px] font-mono text-gray-600 dark:text-slate-400 border border-gray-200/40 dark:border-slate-750">{db}</span>
                      ))
                    ) : (
                      <span className="text-gray-400 italic">None</span>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <p className="text-xs text-gray-400 italic">No execution details available.</p>
            )}
          </div>

          {/* 6. DuckDB Cache Visibility */}
          <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-xs font-bold text-gray-900 dark:text-slate-100 uppercase tracking-wider text-blue-600 dark:text-blue-400">6. DuckDB Session Cache Tables</h4>
              <span className="text-[10px] font-mono text-gray-400 dark:text-slate-500">Active Tables: {cacheCatalog?.length || 0}</span>
            </div>
            
            <div className="overflow-x-auto rounded-xl border border-gray-100 dark:border-slate-850 max-h-64">
              <table className="w-full text-[11px] text-left">
                <thead className="bg-slate-50 dark:bg-slate-950/60 sticky top-0 border-b border-gray-100 dark:border-slate-850 font-bold text-gray-400 uppercase text-[9px] tracking-wider">
                  <tr>
                    <th className="py-2.5 px-3">Node ID</th>
                    <th className="py-2.5 px-3">Table Name</th>
                    <th className="py-2.5 px-3 text-right">Row Count</th>
                    <th className="py-2.5 px-3 text-right">Memory Size</th>
                    <th className="py-2.5 px-3">Created</th>
                    <th className="py-2.5 px-3">Last Accessed</th>
                    <th className="py-2.5 px-3 text-center">Reuse Count</th>
                    <th className="py-2.5 px-3 text-center">In Memory</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-slate-850 font-medium text-gray-800 dark:text-slate-350">
                  {cacheCatalog && cacheCatalog.length > 0 ? (
                    cacheCatalog.map((cache: any, idx: number) => (
                      <tr key={`${cache.node_id}-${idx}`} className="hover:bg-slate-50/50 dark:hover:bg-slate-950/30 transition-colors">
                        <td className="py-2 px-3 font-mono">Node {cache.node_id}</td>
                        <td className="py-2 px-3 font-mono text-[10px] text-blue-600 dark:text-blue-400 select-all">{cache.table_name}</td>
                        <td className="py-2 px-3 text-right font-mono">{cache.row_count?.toLocaleString()}</td>
                        <td className="py-2 px-3 text-right font-mono">
                          {cache.memory_size >= 1024 * 1024 
                            ? `${(cache.memory_size / (1024 * 1024)).toFixed(1)} MB` 
                            : cache.memory_size >= 1024 
                              ? `${(cache.memory_size / 1024).toFixed(1)} KB` 
                              : `${cache.memory_size} B`}
                        </td>
                        <td className="py-2 px-3 text-gray-400 dark:text-slate-500">
                          {cache.creation_time ? new Date(cache.creation_time * 1000).toLocaleTimeString() : "N/A"}
                        </td>
                        <td className="py-2 px-3 text-gray-400 dark:text-slate-500">
                          {cache.last_accessed ? new Date(cache.last_accessed * 1000).toLocaleTimeString() : "N/A"}
                        </td>
                        <td className="py-2 px-3 text-center font-mono font-bold text-emerald-600 dark:text-emerald-400">{cache.reuse_count}</td>
                        <td className="py-2 px-3 text-center">
                          {cache.in_memory === true ? (
                            <span className="px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400">LIVE</span>
                          ) : cache.in_memory === false ? (
                            <span className="px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400">SQLite only</span>
                          ) : (
                            <span className="px-1.5 py-0.5 rounded-full text-[9px] font-bold bg-gray-100 dark:bg-slate-800 text-gray-500">?</span>
                          )}
                        </td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={8} className="py-6 px-3 text-center text-gray-400 dark:text-slate-500 italic bg-slate-50/20 dark:bg-slate-950/10">
                        No cached tables found for this session. Run a query with a metric or timeframe filter to populate cache.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* 9. Workspace Semantic Intelligence — Phase 7E */}
          {semantic_report && (
            <div className="bg-white dark:bg-slate-900 border border-indigo-100 dark:border-indigo-900/40 shadow-sm rounded-2xl p-5">
              <div className="flex items-center justify-between mb-4">
                <h4 className="text-xs font-bold text-indigo-600 dark:text-indigo-400 uppercase tracking-wider flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full bg-indigo-500"></span>
                  9. Workspace Semantic Intelligence
                </h4>
                <span className="text-[10px] font-bold px-2 py-1 rounded-full bg-indigo-50 dark:bg-indigo-950/40 text-indigo-600 dark:text-indigo-400 border border-indigo-100 dark:border-indigo-900/30">
                  {semantic_report.workspace}
                </span>
              </div>

              {/* Confidence Score */}
              <div className="mb-4">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider">Semantic Confidence</span>
                  <span className={`text-sm font-bold ${
                    (semantic_report.semantic_confidence || 0) >= 0.8
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : (semantic_report.semantic_confidence || 0) >= 0.5
                      ? 'text-amber-600 dark:text-amber-400'
                      : 'text-red-600 dark:text-red-400'
                  }`}>
                    {((semantic_report.semantic_confidence || 0) * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="w-full bg-gray-100 dark:bg-slate-800 rounded-full h-2">
                  <div
                    className={`h-2 rounded-full transition-all ${
                      (semantic_report.semantic_confidence || 0) >= 0.8
                        ? 'bg-emerald-500'
                        : (semantic_report.semantic_confidence || 0) >= 0.5
                        ? 'bg-amber-500'
                        : 'bg-red-500'
                    }`}
                    style={{ width: `${((semantic_report.semantic_confidence || 0) * 100).toFixed(0)}%` }}
                  />
                </div>
              </div>

              {/* Stats Grid */}
              <div className="grid grid-cols-3 md:grid-cols-6 gap-3 mb-4">
                {[
                  { label: 'Columns',    value: semantic_report.columns },
                  { label: 'Metrics',    value: semantic_report.metrics_detected },
                  { label: 'Dimensions', value: semantic_report.dimensions_detected },
                  { label: 'Time Cols',  value: semantic_report.time_columns },
                  { label: 'Identifiers', value: semantic_report.identifier_columns },
                  { label: 'Aliases',    value: semantic_report.aliases_generated },
                ].map(({ label, value }) => (
                  <div key={label} className="bg-slate-50/70 dark:bg-slate-950/25 rounded-xl p-2.5 border border-gray-100 dark:border-slate-800 text-center">
                    <span className="block text-[9px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-0.5">{label}</span>
                    <span className="text-lg font-bold text-gray-900 dark:text-slate-100">{value ?? '—'}</span>
                  </div>
                ))}
              </div>

              {/* Warnings */}
              {semantic_report.warnings && semantic_report.warnings.length > 0 && (
                <div className="mb-4 space-y-1.5">
                  {semantic_report.warnings.map((w: string, i: number) => (
                    <div key={i} className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/20 border border-amber-100 dark:border-amber-900/30 rounded-xl px-3 py-2">
                      <span className="mt-0.5 flex-shrink-0">⚠</span>
                      <span>{w}</span>
                    </div>
                  ))}
                </div>
              )}

              {/* Column Breakdown Table */}
              {semantic_report.column_breakdown && (
                <div>
                  <p className="text-[10px] font-bold text-gray-400 dark:text-slate-500 uppercase tracking-wider mb-2">Column Breakdown</p>
                  <div className="overflow-x-auto rounded-xl border border-gray-100 dark:border-slate-850 max-h-64">
                    <table className="w-full text-[11px] text-left">
                      <thead className="bg-slate-50 dark:bg-slate-950/60 sticky top-0 border-b border-gray-100 dark:border-slate-850 font-bold text-gray-400 uppercase text-[9px] tracking-wider">
                        <tr>
                          <th className="py-2 px-3">Column</th>
                          <th className="py-2 px-3">Class</th>
                          <th className="py-2 px-3">Resolved As</th>
                          <th className="py-2 px-3 text-center">Aliases</th>
                          <th className="py-2 px-3">Unit</th>
                          <th className="py-2 px-3">Agg</th>
                          <th className="py-2 px-3">Sample Aliases</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100 dark:divide-slate-850 font-medium text-gray-700 dark:text-slate-350">
                        {Object.entries(semantic_report.column_breakdown).map(([col, info]: [string, any]) => (
                          <tr key={col} className="hover:bg-slate-50/40 dark:hover:bg-slate-950/20 transition-colors">
                            <td className="py-2 px-3 font-mono text-indigo-600 dark:text-indigo-400">{col}</td>
                            <td className="py-2 px-3">
                              <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded-md ${
                                info.classification === 'METRIC' ? 'bg-blue-50 dark:bg-blue-950/30 text-blue-600 dark:text-blue-400' :
                                info.classification === 'DIMENSION' ? 'bg-emerald-50 dark:bg-emerald-950/30 text-emerald-600 dark:text-emerald-400' :
                                info.classification === 'TIME' ? 'bg-purple-50 dark:bg-purple-950/30 text-purple-600 dark:text-purple-400' :
                                info.classification === 'IDENTIFIER' ? 'bg-gray-100 dark:bg-slate-800 text-gray-500 dark:text-slate-400' :
                                'bg-amber-50 dark:bg-amber-950/30 text-amber-600 dark:text-amber-400'
                              }`}>{info.classification}</span>
                            </td>
                            <td className="py-2 px-3 text-gray-800 dark:text-slate-200 font-semibold">{info.resolved_as}</td>
                            <td className="py-2 px-3 text-center font-mono">{info.aliases_count}</td>
                            <td className="py-2 px-3 text-gray-500 dark:text-slate-400">{info.unit || '—'}</td>
                            <td className="py-2 px-3 text-gray-500 dark:text-slate-400">{info.aggregation || '—'}</td>
                            <td className="py-2 px-3 text-gray-400 dark:text-slate-500 truncate max-w-[180px]" title={info.aliases_preview?.join(', ')}>
                              {info.aliases_preview?.slice(0, 3).join(', ')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Generated SQL */}
          {sql_query && (
            <div className="grid grid-cols-1 gap-4">
              <div className="bg-white dark:bg-slate-900 border border-gray-100 dark:border-slate-800 shadow-sm rounded-2xl p-5 flex flex-col">
                <h3 className="text-xs font-bold text-gray-900 dark:text-slate-100 mb-3 uppercase tracking-wider text-blue-600 dark:text-blue-400">Generated SQL Query</h3>
                <div className="bg-gray-50 dark:bg-slate-950 border border-gray-100 dark:border-slate-850 rounded-xl p-4 overflow-x-auto max-h-60">
                  <pre className="text-xs leading-relaxed text-gray-800 dark:text-slate-350 font-mono select-all">
                    {sql_query.split('\n').map((line: string, i: number) => (
                      <div key={i} className="flex">
                        <span className="w-6 text-gray-400 dark:text-slate-600 select-none mr-2">{i + 1}</span>
                        <span dangerouslySetInnerHTML={{
                          __html: line
                            .replace(/(SELECT|FROM|WHERE|GROUP BY|ORDER BY|AND|OR|AS|SUM)/g, '<span class="text-blue-600 dark:text-blue-400 font-semibold">$1</span>')
                            .replace(/('[^']*')/g, '<span class="text-emerald-600 dark:text-emerald-400">$1</span>')
                        }} />
                      </div>
                    ))}
                  </pre>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
