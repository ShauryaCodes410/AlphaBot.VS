'use client';

import { useState, useEffect } from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from 'chart.js';
import { Bar, Line, Doughnut } from 'react-chartjs-2';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler
);

const formatLabel = (label: any): string => {
  if (label == null) return "";
  let str = String(label).trim();

  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  if (/^(0?[1-9]|1[0-2])$/.test(str)) {
    const idx = parseInt(str, 10) - 1;
    return months[idx] || str;
  }

  const matchYearMonth = str.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
  if (matchYearMonth) {
    const [_, year, month] = matchYearMonth;
    const idx = parseInt(month, 10) - 1;
    return `${months[idx]} ${year}`;
  }

  // snake_case → Title Case  (e.g. "diablo_canyon" → "Diablo Canyon")
  if (str.includes('_')) {
    return str.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  }

  // lowercase single word → Title Case  (e.g. "central" → "Central")
  if (/^[a-z]+$/.test(str)) {
    return str.charAt(0).toUpperCase() + str.slice(1);
  }

  return str;
};

const formatSingleValue = (val: number, metricName: string, unit: string) => {
  const mLower = metricName.toLowerCase();
  
  const isCurrency = 
    ['revenue', 'budget', 'cost', 'profit', 'spend', 'expense', 'salary', 'tax', 'payment'].some(k => mLower.includes(k)) ||
    ['usd', 'inr'].includes(unit.toLowerCase());

  if (isCurrency) {
    const prefix = "₹"; // Indian context for plants in Gujarat, Rajasthan etc.
    if (val >= 1000000) return `${prefix}${(val / 1000000).toFixed(1)}M`;
    if (val >= 1000) return `${prefix}${(val / 1000).toFixed(1)}K`;
    return `${prefix}${val.toLocaleString()}`;
  }

  if (mLower.includes('capacity') || mLower.includes('mw') || unit === 'MW') {
    return `${val.toLocaleString()} MW`;
  }
  
  if (mLower.includes('percent') || mLower.includes('pct') || unit === 'PERCENT') {
    return `${val.toLocaleString()}%`;
  }
  
  if (mLower.includes('delay') || mLower.includes('days') || unit === 'DAYS') {
    return `${val.toLocaleString()} Days`;
  }
  
  if (unit && unit !== 'RawData' && unit !== 'Units') {
    return `${val.toLocaleString()} ${unit}`;
  }
  
  return val.toLocaleString();
};

/** Determine the single best chart type for a given result set */
function pickBestChart(
  results: any[],
  labelKey: string | undefined,
  numericKeys: string[],
  isTimeSeries: boolean
): 'line' | 'bar' | 'doughnut' {
  // Time-series data → line chart (best for trends)
  if (isTimeSeries) return 'line';

  // Multiple numeric columns in same row (multi-metric comparison) → grouped bar
  if (numericKeys.length > 1) return 'bar';

  // Small categorical breakdown (≤ 4 items) → doughnut (easy proportion reading)
  if (results.length <= 4 && labelKey) return 'doughnut';

  // Larger categorical breakdown → horizontal bar
  return 'bar';
}

/** Determine the second-best alternative chart type */
function pickAlternativeChart(
  results: any[],
  labelKey: string | undefined,
  numericKeys: string[],
  isTimeSeries: boolean
): 'line' | 'bar' | 'doughnut' {
  // Time-series data → bar chart
  if (isTimeSeries) return 'bar';

  // Multiple numeric columns in same row → line chart
  if (numericKeys.length > 1) return 'line';

  // Small categorical breakdown → bar chart
  if (results.length <= 8 && labelKey) return 'bar';

  // Larger categorical breakdown → line chart
  return 'line';
}

interface DataChartProps {
  results: any[];
  unit: string;
  darkMode?: boolean;
  title?: string;
}

export function DataChart({ results, unit, darkMode, title }: DataChartProps) {
  const [timeAgg, setTimeAgg] = useState<'detailed' | 'yearly'>('detailed');
  const [chartMode, setChartMode] = useState<'best' | 'alternative'>('best');

  useEffect(() => {
    const firstRow = results[0] || {};
    const keys = Object.keys(firstRow);
    const numKeys = keys.filter(key => typeof firstRow[key] === 'number');
    const isMultiYear = numKeys.every(k => /^(19|20)\d{2}$/.test(k)) && numKeys.length > 0;

    if (isMultiYear && numKeys.length > 2) {
      setTimeAgg('yearly');
    } else {
      setTimeAgg('detailed');
    }
    setChartMode('best');
  }, [results]);

  if (!results || results.length === 0) {
    return <p className="text-gray-500 dark:text-slate-400 text-sm">No data to display</p>;
  }

  const firstRow = results[0];
  const keys = Object.keys(firstRow);
  const numericKeys = keys.filter(key => typeof firstRow[key] === 'number');
  const labelKey = keys.find(key => typeof firstRow[key] === 'string');

  // ── Single scalar value ──────────────────────────────────────────────────
  if (results.length === 1 && numericKeys.length === 1) {
    const value = firstRow[numericKeys[0]];
    const rawMetric = numericKeys[0];
    const metricName = rawMetric.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    const labelVal = labelKey ? firstRow[labelKey] : null;
    const formattedLabel = labelVal ? formatLabel(labelVal) : "";
    const displayValue = formatSingleValue(value, rawMetric, unit);
    
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center w-full">
        <div className="text-6xl font-extrabold text-slate-900 dark:text-slate-50 tracking-tight mb-4 animate-fade-in">
          {displayValue}
        </div>
        <div className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-widest bg-slate-100 dark:bg-slate-800/80 px-3.5 py-1.5 rounded-full border border-slate-200/50 dark:border-slate-700/50 shadow-sm">
          {metricName}{formattedLabel ? ` • ${formattedLabel}` : ""}
        </div>
      </div>
    );
  }

  const isTimeSeries = results.length > 1 && labelKey != null && labelKey.includes('date');
  const isMultiYearComparison = isTimeSeries && numericKeys.length > 0 && numericKeys.every(k => /^(19|20)\d{2}$/.test(k));
  const roundToTwo = (num: number) => Math.round((num + Number.EPSILON) * 100) / 100;

  // ── Time-aggregation (for monthly data spanning multiple years) ───────────
  let displayResults = results;
  let finalLabelKey = labelKey;
  let finalNumericKeys = numericKeys;

  if (isTimeSeries && timeAgg === 'yearly') {
    if (isMultiYearComparison) {
      // Pivot year columns into a single metric trend over years
      const yearlySums: Record<string, number> = {};
      numericKeys.forEach(yr => {
        yearlySums[yr] = 0;
      });
      results.forEach(row => {
        numericKeys.forEach(yr => {
          yearlySums[yr] += (row[yr] || 0);
        });
      });
      
      displayResults = numericKeys.map(yr => ({
        year: yr,
        value: roundToTwo(yearlySums[yr])
      }));
      finalLabelKey = "year";
      finalNumericKeys = ["value"];
    } else if (labelKey) {
      const aggMap = new Map<string, any>();
      results.forEach(r => {
        const yearStr = String(r[labelKey]).substring(0, 4);
        if (!aggMap.has(yearStr)) {
          aggMap.set(yearStr, { [labelKey]: yearStr, ...Object.fromEntries(numericKeys.map(k => [k, 0])) });
        }
        const row = aggMap.get(yearStr);
        numericKeys.forEach(k => { row[k] += (r[k] || 0); });
      });
      displayResults = Array.from(aggMap.values());
    }
  }

  const bestChart = pickBestChart(displayResults, finalLabelKey, finalNumericKeys, isTimeSeries);
  const alternativeChart = pickAlternativeChart(displayResults, finalLabelKey, finalNumericKeys, isTimeSeries);
  const activeChart = chartMode === 'best' ? bestChart : alternativeChart;

  // shared palette - rich and professional
  const borderColors = [
    'rgb(16, 185, 129)', 'rgb(96, 165, 250)', 'rgb(217, 164, 65)',
    'rgb(45, 212, 191)', 'rgb(148, 163, 184)', 'rgb(52, 211, 153)',
    'rgb(125, 211, 252)', 'rgb(251, 191, 36)', 'rgb(110, 231, 183)', 'rgb(147, 197, 253)'
  ];

  // Dynamic colors for dark mode support
  const isDark = !!darkMode;
  const textColor = isDark ? 'rgba(168, 178, 172, 0.95)' : 'rgba(71, 85, 105, 0.9)';
  const gridColor = isDark ? 'rgba(255, 255, 255, 0.06)' : 'rgba(226, 232, 240, 0.8)';
  const legendColor = isDark ? 'rgba(231, 238, 234, 0.95)' : 'rgba(15, 23, 42, 0.9)';

  // Dynamic scale assignment (Dual Y-Axis and logarithmic checks)
  let dynamicScales: any = {
    x: {
      grid: { color: gridColor },
      ticks: {
        color: textColor,
        font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
      }
    },
    y: {
      grid: { color: gridColor },
      ticks: {
        color: textColor,
        font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
      }
    }
  };

  let dualAxisConfig: { enabled: boolean; largerKey?: string; smallerKey?: string } = { enabled: false };
  let logScaleEnabled = false;

  if (displayResults.length > 1) {
    if (finalNumericKeys.length === 2) {
      const [keyA, keyB] = finalNumericKeys;
      let maxA = 0;
      let maxB = 0;
      displayResults.forEach(r => {
        const valA = Math.abs(r[keyA] || 0);
        const valB = Math.abs(r[keyB] || 0);
        if (valA > maxA) maxA = valA;
        if (valB > maxB) maxB = valB;
      });
      if (maxA > 0 && maxB > 0) {
        const larger = Math.max(maxA, maxB);
        const smaller = Math.min(maxA, maxB);
        if (larger / smaller > 50) {
          dualAxisConfig = {
            enabled: true,
            largerKey: maxA > maxB ? keyA : keyB,
            smallerKey: maxA > maxB ? keyB : keyA
          };
          
          const prettyAxis = (key: string) => key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
          const labelLarger = prettyAxis(dualAxisConfig.largerKey || '');
          const labelSmaller = prettyAxis(dualAxisConfig.smallerKey || '');

          dynamicScales = {
            x: {
              grid: { color: gridColor },
              ticks: {
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
              }
            },
            y: {
              type: 'linear' as const,
              display: true,
              position: 'left' as const,
              grid: { color: gridColor },
              ticks: {
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
              },
              title: {
                display: true,
                text: labelLarger,
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 600, size: 10 }
              }
            },
            y1: {
              type: 'linear' as const,
              display: true,
              position: 'right' as const,
              grid: { drawOnChartArea: false },
              ticks: {
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
              },
              title: {
                display: true,
                text: labelSmaller,
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 600, size: 10 }
              }
            }
          };
        }
      }
    } else if (finalNumericKeys.length === 1) {
      let maxVal = -Infinity;
      let minVal = Infinity;
      displayResults.forEach(r => {
        const val = r[finalNumericKeys[0]];
        if (typeof val === 'number') {
          if (val > maxVal) maxVal = val;
          if (val > 0 && val < minVal) minVal = val;
        }
      });
      if (maxVal > 0 && minVal < Infinity && minVal > 0) {
        if (maxVal / minVal > 1000) {
          logScaleEnabled = true;
          dynamicScales = {
            x: {
              grid: { color: gridColor },
              ticks: {
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
              }
            },
            y: {
              type: 'logarithmic' as const,
              grid: { color: gridColor },
              ticks: {
                color: textColor,
                font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 10 }
              }
            }
          };
        }
      }
    }
  }

  const compactTick = (value: unknown) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value ?? '');
    const abs = Math.abs(n);
    if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (abs >= 10_000) return `${Math.round(n / 1_000)}K`;
    return n.toLocaleString();
  };
  if (dynamicScales.y?.ticks) {
    dynamicScales.y.ticks.maxTicksLimit = 6;
    dynamicScales.y.ticks.callback = compactTick;
  }
  if (dynamicScales.y1?.ticks) {
    dynamicScales.y1.ticks.maxTicksLimit = 6;
    dynamicScales.y1.ticks.callback = compactTick;
  }
  const scales = dynamicScales;

  // ── Time-agg toggle (only shown when data has detailed dates or is a multi-year comparison) ────────────
  const showAggToggle = isTimeSeries && labelKey && (String(firstRow[labelKey]).length > 4 || isMultiYearComparison);

  const renderVisualizationHeader = () => {
    const hasChartToggle = bestChart !== alternativeChart;
    const heading = (title || 'Result').replace(/_/g, ' ');

    return (
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-3 dark:border-white/10">
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium capitalize text-slate-900 dark:text-white" title={heading}>
          {heading}
        </h3>
        <div className="flex flex-wrap items-center gap-2">
          {showAggToggle && (
            <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 dark:border-white/10 dark:bg-white/[0.04]">
              <button
                type="button"
                onClick={() => setTimeAgg('detailed')}
                className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                  timeAgg === 'detailed'
                    ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-slate-100'
                    : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                Detailed
              </button>
              <button
                type="button"
                onClick={() => setTimeAgg('yearly')}
                className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                  timeAgg === 'yearly'
                    ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-slate-100'
                    : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                Yearly
              </button>
            </div>
          )}
          {hasChartToggle && (
            <div className="inline-flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 dark:border-white/10 dark:bg-white/[0.04]">
              <button
                type="button"
                onClick={() => setChartMode('best')}
                className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                  chartMode === 'best'
                    ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-slate-100'
                    : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                {bestChart.charAt(0).toUpperCase() + bestChart.slice(1)}
              </button>
              <button
                type="button"
                onClick={() => setChartMode('alternative')}
                className={`rounded-md px-3 py-1.5 text-xs font-medium ${
                  chartMode === 'alternative'
                    ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-slate-100'
                    : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                {alternativeChart.charAt(0).toUpperCase() + alternativeChart.slice(1)}
              </button>
            </div>
          )}
        </div>
      </div>
    );
  };

  // ── Single-row / multi-metric (e.g. "show all metrics for 2026") ─────────
  if (results.length === 1 && numericKeys.length > 1) {
    const data = {
      labels: numericKeys.map(k => k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())),
      datasets: [{
        label: unit,
        data: numericKeys.map(k => firstRow[k]),
        backgroundColor: activeChart === 'doughnut'
          ? borderColors.map(c => c.replace('rgb', 'rgba').replace(')', ', 0.82)'))
          : activeChart === 'line'
            ? 'rgba(16, 185, 129, 0.08)'
            : borderColors.map(c => c.replace('rgb', 'rgba').replace(')', ', 0.82)')),
        borderColor: activeChart === 'doughnut' ? borderColors : 'rgb(16, 185, 129)',
        borderWidth: activeChart === 'line' ? 2 : 1,
        fill: activeChart === 'line',
        tension: 0.35,
      }],
    };

    const isHorizontalBar = activeChart === 'bar';
    const options = {
      indexAxis: isHorizontalBar ? ('y' as const) : ('x' as const),
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 8, right: 12, bottom: 4, left: 4 } },
      ...(activeChart !== 'doughnut' ? { scales } : {}),
      plugins: {
        legend: {
          display: activeChart === 'doughnut',
          labels: {
            color: legendColor,
            font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 11 },
            boxWidth: 8,
            boxHeight: 8,
            padding: 12
          }
        },
        tooltip: {
          callbacks: {
            label: (c: any) => {
              const val = activeChart === 'doughnut' ? c.parsed : (isHorizontalBar ? c.parsed.x : c.parsed.y);
              return `${c.label}: ${val?.toLocaleString()} ${unit}`;
            }
          }
        },
      },
    };

    return (
      <div className="w-full">
        {renderVisualizationHeader()}
        <div className="h-[390px] pt-1">
          {activeChart === 'line'    && <Line     data={data} options={options as any} />}
          {activeChart === 'bar'     && <Bar      data={data} options={options as any} />}
          {activeChart === 'doughnut'&& <Doughnut data={data} options={options as any} />}
        </div>
      </div>
    );
  }

  // ── Multi-row data with a label key ─────────────────────────────────────
  if (displayResults.length >= 1 && finalLabelKey) {
    const labels = displayResults.map(r => formatLabel(r[finalLabelKey]));

    // Build datasets
    const datasets = finalNumericKeys.map((metric, idx) => {
      const color = borderColors[idx % borderColors.length];
      const isLine = activeChart === 'line';
      const bg = isLine
        ? color.replace('rgb', 'rgba').replace(')', ', 0.08)')
        : color.replace('rgb', 'rgba').replace(')', ', 0.82)');
      return {
        label: metric === 'value' ? (unit || 'Total') : metric.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
        data: displayResults.map(r => r[metric]),
        backgroundColor: activeChart === 'doughnut'
          ? borderColors.map(c => c.replace('rgb', 'rgba').replace(')', ', 0.82)'))
          : bg,
        borderColor: activeChart === 'doughnut' ? borderColors : color,
        borderWidth: isLine ? 2 : 1,
        fill: isLine,
        tension: 0.35,
        pointRadius: isLine ? 4 : undefined,
        pointHoverRadius: isLine ? 6 : undefined,
        ...(dualAxisConfig.enabled ? { yAxisID: metric === dualAxisConfig.largerKey ? 'y' : 'y1' } : {}),
      };
    });

    const chartData = { labels, datasets };

    const baseOptions = {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 8, right: 12, bottom: 4, left: 4 } },
      ...(activeChart !== 'doughnut' ? { scales } : {}),
      plugins: {
        legend: {
          display: activeChart === 'doughnut' || activeChart === 'line' || finalNumericKeys.length > 1,
          position: activeChart === 'doughnut' ? ('right' as const) : ('top' as const),
          labels: {
            color: legendColor,
            font: { family: 'Inter, system-ui, sans-serif', weight: 500, size: 11 },
            boxWidth: 8,
            boxHeight: 8,
            padding: 12
          }
        },
        tooltip: {
          callbacks: {
            label: (c: any) => {
              const val = activeChart === 'doughnut' ? c.parsed : c.parsed.y;
              return `${c.dataset.label || c.label}: ${val?.toLocaleString()} ${unit}`;
            },
          },
        },
      },
    };

    // horizontal bar for large breakdowns, regular vertical for comparisons
    const barOptions = activeChart === 'bar' && displayResults.length > 8
      ? { ...baseOptions, indexAxis: 'y' as const }
      : baseOptions;

    return (
      <div className="w-full">
        {renderVisualizationHeader()}
        <div className="h-[390px] pt-1">
          {activeChart === 'line'    && <Line     data={chartData} options={baseOptions as any} />}
          {activeChart === 'bar'     && <Bar      data={chartData} options={barOptions  as any} />}
          {activeChart === 'doughnut'&& <Doughnut data={chartData} options={baseOptions as any} />}
        </div>
      </div>
    );
  }

  // ── Fallback ─────────────────────────────────────────────────────────────
  return (
    <div className="text-gray-700 dark:text-slate-350">
      <pre className="text-xs">{JSON.stringify(results, null, 2)}</pre>
    </div>
  );
}
