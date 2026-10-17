import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import type { ConversationContext } from '@/lib/session';

interface QueryInterfaceProps {
  onSubmit: (query: string) => void;
  isLoading: boolean;
  recentQueries?: {query: string, result: any}[];
  onSelectRecent?: (item: {query: string, result: any}) => void;
  liveKpis?: Record<string, number> | null;
  onQueryChange?: (q: string) => void;
  results?: any;
  conversationContext?: ConversationContext | null;
  devMode?: boolean;
  onClearSession?: () => void;
  queryOverride?: string | null;
  onClearOverride?: () => void;
  metadataMetrics: string[];
  metadataDepts: string[];
  metadataRegions: string[];
  metadataYears?: string[];
  metadataGroups?: { title: string; items: string[] }[];
  suggestedQueries?: string[];
  selectedDb?: string;
}

interface CategorizedSuggestions {
  metrics: string[];
  analysis: string[];
  comparisons: string[];
}

interface Preview {
  intent: string;
  metric: string;
  dimension: string;
}

const KNOWN_METRICS = [
  'operating_cost',
  'marketing_spend',
  'tax_liability',
  'asset_value',
  'customer_count',
  'revenue',
  'profit',
  'expenses',
  'headcount',
  'salary',
  // schema-level
  'capacity_mw',
  'budget_allocated',
  'budget_used',
  'budget_remaining',
  'completion_percentage',
  'delay_days',
  'payment_received',
];

// All completable tokens (metrics + intent words + dimension words)
// Will be merged with server metadata at runtime
const STATIC_COMPLETIONS = [
  ...KNOWN_METRICS.map(m => m.replace(/_/g, ' ')),
  // intents
  'trend', 'compare', 'breakdown', 'top', 'highest', 'lowest',
  // dimensions
  'by state', 'by plant', 'by project type', 'by contractor', 'by year',
  'across states', 'across plants',
  // time
  '2022', '2023', '2024', '2025', '2026',
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

export function QueryInterface({ onSubmit, isLoading, recentQueries, onSelectRecent, liveKpis, onQueryChange, results, conversationContext, devMode = false, onClearSession, queryOverride, onClearOverride, metadataMetrics, metadataDepts, metadataRegions, metadataYears = [], metadataGroups = [], suggestedQueries = [], selectedDb = 'default' }: QueryInterfaceProps) {
  const [query, setQuery] = useState('');
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [categorizedSuggestions, setCategorizedSuggestions] = useState<CategorizedSuggestions>({
    metrics: [], analysis: [], comparisons: []
  });
  const [preview, setPreview] = useState<Preview | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [lastSubmitted, setLastSubmitted] = useState('');
  const [isFocused, setIsFocused] = useState(false);
  // Merged completions list (static + server metadata, capped for perf)
  const completionsRef = useRef<string[]>(STATIC_COMPLETIONS.slice(0, 80));
  // Prevents suggest fetch when loading from recent (result already displayed)
  const skipSuggestRef = useRef(false);

  const getFollowUpQuestions = () => {
    const pretty = (name: string) => name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    const questionsFor = (metricName: string) => {
      const capM = pretty(metricName);
      const questions: string[] = [`${capM} trend`];
      if (metadataYears.length >= 2) {
        questions.push(`Compare ${capM} in ${metadataYears[0]} and ${metadataYears[metadataYears.length - 1]}`);
      } else if (metadataYears.length === 1) {
        questions.push(`${capM} in ${metadataYears[0]}`);
      }
      const group = metadataGroups.find(item => item.items.length > 0);
      if (group) questions.push(`${capM} by ${group.title}`);
      const alt = metadataMetrics.find(item => item !== metricName);
      if (alt) questions.push(`Show ${pretty(alt)} instead`);
      return questions.slice(0, 5);
    };

    // Priority 1: use server-side conversation context (accurate, cross-refresh)
    if (conversationContext?.active_metric) {
      return questionsFor(conversationContext.active_metric);
    }

    if (!results || !results.results) return [];
    const detectedMetrics = extractMetrics(results.sql_query, results.results);
    const metricName = detectedMetrics[0] || metadataMetrics[0] || '';
    if (!metricName) return [];
    return questionsFor(metricName);
  };

  const metrics = metadataMetrics.map((x: string) => x.replace(/_/g, ' '));

  const inputRef = useRef<HTMLInputElement>(null);
  const suggestionListRef = useRef<HTMLDivElement>(null);
  const tagsRef = useRef<HTMLDivElement>(null);

  // Synchronize queryOverride prop to local query state
  useEffect(() => {
    if (queryOverride !== undefined && queryOverride !== null) {
      setQuery(queryOverride);
      setSelectedIndex(-1);
      // Reset dedup guard so the overridden query always executes
      setLastSubmitted('');
      if (onClearOverride) {
        onClearOverride();
      }
    }
  }, [queryOverride, onClearOverride]);

  // Synchronize metadata props to completions list
  useEffect(() => {
    if (metadataMetrics.length > 0) {
      const serverMetrics = metadataMetrics.map((m: string) => m.replace(/_/g, ' '));
      const serverCats = [...metadataDepts, ...metadataRegions]
        .map((v: string) => String(v).toLowerCase())
        .slice(0, 40);
      
      completionsRef.current = Array.from(new Set([
        ...serverMetrics,
        ...metadataYears,
        ...serverCats,
        'trend', 'compare', 'breakdown', 'by year',
      ])).slice(0, 80);
    }
  }, [metadataMetrics, metadataDepts, metadataRegions, metadataYears]);

  // Live suggestions as user types (120ms debounce)
  useEffect(() => {
    // Skip suggest fetch if this query was loaded from recent cache
    if (skipSuggestRef.current) {
      skipSuggestRef.current = false;
      return;
    }
    if (!query.trim()) {
      setCategorizedSuggestions({ metrics: [], analysis: [], comparisons: [] });
      setPreview(null);
      setShowSuggestions(false);
      setSelectedIndex(-1);
      return;
    }
    setCategorizedSuggestions({ metrics: [], analysis: [], comparisons: [] });
    setShowSuggestions(false);
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const res = await fetch(`/api/suggest?q=${encodeURIComponent(query)}&db=${encodeURIComponent(selectedDb)}`, { credentials: 'include' });
        if (!cancelled && res.ok) {
          const data = await res.json();
          if (!cancelled) {
            setCategorizedSuggestions(data.suggestions);
            setPreview(data.preview);
            if (isFocused || document.activeElement === inputRef.current) {
              setShowSuggestions(true);
            }
          }
        }
      } catch {}
    }, 200);
    return () => { cancelled = true; clearTimeout(t); };
  }, [query, isFocused, selectedDb]);

  // Ghost text: derive from query synchronously — useMemo avoids extra re-render
  const ghostText = useMemo(() => {
    if (!query) return '';
    const lastSpaceIdx = query.lastIndexOf(' ');
    const lastWord = lastSpaceIdx === -1 ? query : query.slice(lastSpaceIdx + 1);
    if (lastWord.length < 2) return '';
    const lw = lastWord.toLowerCase();
    const match = completionsRef.current.find(
      c => c.toLowerCase().startsWith(lw) && c.toLowerCase() !== lw
    );
    return match ? match.slice(lastWord.length) : '';
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // Accept ghost text on Tab or ArrowRight at end of input
  const acceptGhost = useCallback(() => {
    if (!ghostText) return false;
    const lastSpaceIdx = query.lastIndexOf(' ');
    const prefix = lastSpaceIdx === -1 ? '' : query.slice(0, lastSpaceIdx + 1);
    const lastWord = lastSpaceIdx === -1 ? query : query.slice(lastSpaceIdx + 1);
    const completed = prefix + lastWord + ghostText;
    setQuery(completed);
    if (onQueryChange) onQueryChange(completed);
    return true;
  }, [ghostText, query, onQueryChange]);

  const runQuery = useCallback((q: string) => {
    const trimmed = q.trim();
    if (!trimmed || isLoading) return;
    setLastSubmitted(trimmed);
    setShowSuggestions(false);
    onSubmit(trimmed);
  }, [isLoading, onSubmit]);

  const handleTextChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setQuery(val);
    setSelectedIndex(-1);
    setShowSuggestions(true);
    if (onQueryChange) onQueryChange(val);
  };

  const handleTagClick = (tag: string) => {
    const next = (query + ' ' + tag).trim();
    setQuery(next);
    setSelectedIndex(-1);
    setShowSuggestions(true);
    if (onQueryChange) onQueryChange(next);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const flatSuggestions = [
    ...categorizedSuggestions.metrics,
    ...categorizedSuggestions.analysis,
    ...categorizedSuggestions.comparisons,
  ];

  const getFlatIndex = (cat: 'metrics' | 'analysis' | 'comparisons', idx: number) => {
    if (cat === 'metrics') return idx;
    if (cat === 'analysis') return categorizedSuggestions.metrics.length + idx;
    return categorizedSuggestions.metrics.length + categorizedSuggestions.analysis.length + idx;
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // Accept ghost text with Tab or ArrowRight when cursor is at end
    if ((e.key === 'Tab' || (e.key === 'ArrowRight' && (e.target as HTMLInputElement).selectionStart === query.length)) && ghostText) {
      e.preventDefault();
      acceptGhost();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex(p => (p < flatSuggestions.length - 1 ? p + 1 : p));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex(p => (p > 0 ? p - 1 : -1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (selectedIndex >= 0) {
        const s = flatSuggestions[selectedIndex];
        setQuery(s);
        setSelectedIndex(-1);
        runQuery(s);
      } else {
        runQuery(query);
      }
    } else if (e.key === 'Escape') {
      setShowSuggestions(false);
    }
  };

  const handleSuggestionClick = (s: string) => {
    setQuery(s);
    setSelectedIndex(-1);
    if (onQueryChange) onQueryChange(s);
    runQuery(s);
  };

  // Scroll active suggestion into view
  useEffect(() => {
    if (selectedIndex >= 0 && suggestionListRef.current) {
      const items = suggestionListRef.current.querySelectorAll('li');
      (items[selectedIndex] as HTMLElement)?.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedIndex]);

  // Hide on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (inputRef.current?.contains(e.target as Node)) return;
      if (suggestionListRef.current?.contains(e.target as Node)) return;
      setTimeout(() => setShowSuggestions(false), 150);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  return (
    <div>
      {/* Conversation Context Pill — shown when session has active state */}
      {devMode && conversationContext?.active_metric && (
        <div className="mb-4">
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">Context</p>
          <div className="flex flex-wrap items-center gap-1.5">
          <span className="max-w-full truncate rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600 dark:border-white/10 dark:bg-white/[0.04] dark:text-slate-300">
            {conversationContext.active_metric.replace(/_/g, ' ')}
          </span>
          {(conversationContext.active_filters || []).filter(f => ['state','location','region'].includes(f.column)).map((f, idx) => (
            <span key={`${f.column}-${f.value}-${idx}`} className="max-w-full truncate rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600 dark:border-white/10 dark:bg-white/[0.04] dark:text-slate-300">
              {f.value}
            </span>
          ))}
          {conversationContext.active_timeframe?.value && (
            <span className="max-w-full truncate rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600 dark:border-white/10 dark:bg-white/[0.04] dark:text-slate-300">
              {conversationContext.active_timeframe.value}
            </span>
          )}
          {/* Clear session button */}
          <button
            onClick={onClearSession}
            title="Clear conversation context"
            className="ml-auto flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold text-gray-400 dark:text-slate-500 hover:text-red-500 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 border border-transparent hover:border-red-200 dark:hover:border-red-900 transition-all"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"/></svg>
            Clear
          </button>
          </div>
        </div>
      )}

      {/* Search input with ghost text overlay */}
      <div className="relative mb-5 rounded-full border border-slate-200 bg-white shadow-sm focus-within:ring-2 focus-within:ring-emerald-400/20 dark:border-white/10 dark:bg-white/[0.04]">
        <div className="absolute inset-y-0 left-0 pl-4 flex items-center pointer-events-none">
          {isLoading
            ? <svg className="animate-spin h-4 w-4 text-stone-400" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
              </svg>
            : <svg className="h-4 w-4 text-stone-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/>
              </svg>
          }
        </div>

        {/* Ghost text layer — sits exactly behind the input text */}
        {ghostText && isFocused && (
          <div
            aria-hidden="true"
            className="absolute inset-0 pl-10 pr-4 py-2.5 text-sm font-medium pointer-events-none flex items-center overflow-hidden rounded-2xl"
          >
            {/* Invisible spacer matching the typed text */}
            <span className="invisible whitespace-pre">{query}</span>
            {/* Visible ghost suffix */}
            <span className="text-slate-400/60 dark:text-slate-500/70 whitespace-pre select-none">{ghostText}</span>
          </div>
        )}

        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={handleTextChange}
          onFocus={() => {
            setIsFocused(true);
            setShowSuggestions(true);
          }}
          onBlur={() => {
            setIsFocused(false);
            setShowSuggestions(false);
          }}
          onKeyDown={handleKeyDown}
          placeholder={query ? '' : 'Ask about your data…'}
          className="w-full pl-11 pr-4 py-3 text-sm text-slate-800 dark:text-slate-100 placeholder:text-slate-400 rounded-full focus:outline-none"
          style={{ background: 'transparent', position: 'relative', zIndex: 1 }}
        />

        {/* Tab to accept hint */}
        {ghostText && isFocused && (
          <div className="absolute right-3 inset-y-0 flex items-center pointer-events-none">
            <span className="text-[10px] font-semibold text-slate-400/70 dark:text-slate-500/70 bg-slate-100 dark:bg-slate-700/60 px-1.5 py-0.5 rounded border border-slate-200 dark:border-slate-600/50">
              Tab
            </span>
          </div>
        )}

        {/* Suggestions dropdown */}
        {query.trim() && showSuggestions && flatSuggestions.length > 0 && (
          <div
            ref={suggestionListRef}
            className="absolute z-20 left-2 right-2 bg-white dark:bg-slate-900 rounded-2xl shadow-lg mt-2 max-h-72 overflow-y-auto py-2"
          >
            {(['metrics', 'analysis', 'comparisons'] as const).map(cat => {
              const items = categorizedSuggestions[cat];
              if (!items.length) return null;
              const label = cat === 'metrics' ? 'Core Queries' : cat === 'analysis' ? 'Suggested Queries' : 'Comparisons';
              return (
                <div key={cat}>
                  <div className="px-3 py-1 text-xs text-slate-400 dark:text-slate-500">{label}</div>
                  <ul>
                    {items.map((s, idx) => {
                      const fi = getFlatIndex(cat, idx);
                      return (
                        <li
                          key={`${cat}-${idx}`}
                          className={`px-3 py-2 text-sm cursor-pointer rounded-xl mx-1 ${
                            selectedIndex === fi 
                              ? 'bg-emerald-50 text-emerald-950 dark:bg-white/[0.06] dark:text-white' 
                              : 'text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-white/[0.04]'
                          }`}
                          onMouseDown={e => { e.preventDefault(); handleSuggestionClick(s); }}
                        >
                          {s}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {query.trim().length > 0 && (
        <p className="mb-4 text-xs text-stone-500 dark:text-slate-400">
          {isLoading
            ? 'Running'
            : preview
              ? [preview.intent || 'Sum', preview.metric && preview.metric !== 'None' ? preview.metric : '', preview.dimension && preview.dimension !== 'None' ? `by ${preview.dimension}` : ''].filter(Boolean).join(' · ')
              : 'Reading the question'}
        </p>
      )}

      {/* Recent Queries */}
      {recentQueries && recentQueries.length > 0 && (
        <div className="mt-6">
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">Recent queries</p>
          <div className="space-y-0.5">
            {recentQueries.map((item, idx) => {
              const active = query === item.query;
              return (
              <button
                key={idx}
                type="button"
                onClick={() => {
                  skipSuggestRef.current = true;
                  setQuery(item.query);
                  setLastSubmitted(item.query);
                  if (onSelectRecent) onSelectRecent(item);
                }}
                disabled={isLoading}
                className={`block w-full truncate rounded-lg px-2 py-1.5 text-left text-sm ${
                  active
                    ? 'bg-emerald-50 text-emerald-900 dark:bg-emerald-400/10 dark:text-emerald-100'
                    : 'text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/[0.05] dark:hover:text-white'
                }`}
                title={item.query}
              >
                {item.query}
              </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Dynamic Suggested Follow-up Questions in Left Sidebar */}
      {query.trim() !== '' && results && results.results && (
        <div className="mt-6">
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">Suggested queries</p>
          <div className="space-y-0.5">
            {getFollowUpQuestions().map((q, idx) => (
              <button
                key={idx}
                type="button"
                onClick={() => {
                  setQuery(q);
                  setSelectedIndex(-1);
                  if (onQueryChange) onQueryChange(q);
                  runQuery(q);
                }}
                disabled={isLoading}
                className="block w-full truncate rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/[0.05] dark:hover:text-white"
                title={q}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Suggested Questions (empty state) */}
      {query.trim() === '' && suggestedQueries.length > 0 && (
        <div className="mt-6 space-y-0.5">
          <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">Suggested queries</p>
          {suggestedQueries.map((ex, idx) => (
            <button
              key={idx}
              type="button"
              onMouseDown={e => { e.preventDefault(); setQuery(ex); if (onQueryChange) onQueryChange(ex); runQuery(ex); }}
              disabled={isLoading}
              className="block w-full truncate rounded-lg px-2 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-white/[0.05] dark:hover:text-white"
            >
              {ex}
            </button>
          ))}
        </div>
      )}

      <div ref={tagsRef} className="mt-8 space-y-6">
        {[
          { title: 'Metrics', items: metrics },
          { title: 'Years', items: metadataYears },
          ...metadataGroups,
        ].filter(group => group.items.length > 0).map(({ title, items }) => (
          <div key={title}>
            <h3 className="mb-2 text-xs text-slate-500 dark:text-slate-400">{title}</h3>
            <div className="flex flex-wrap gap-1.5">
              {items.map(item => (
                <button
                  key={item}
                  type="button"
                  onClick={() => handleTagClick(item)}
                  title={item}
                  className="max-w-full truncate rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs text-slate-600 hover:border-emerald-300 hover:text-slate-900 dark:border-white/10 dark:bg-white/[0.04] dark:text-slate-300 dark:hover:border-emerald-400/40 dark:hover:text-white"
                >
                  {item}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
