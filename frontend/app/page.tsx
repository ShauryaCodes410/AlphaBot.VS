'use client';

import { useState, useEffect, useRef } from 'react';
import { LandingPage } from '@/components/LandingPage';
import { QueryInterface } from '@/components/QueryInterface';
import { ResultsDisplay } from '@/components/ResultsDisplay';
import { getOrCreateSessionId, clearSession, type ConversationContext } from '@/lib/session';

const parseErrorMessage = (msg: string) => {
  let mainMsg = msg;
  let suggestion: string | null = null;
  let suggestionType: 'did-you-mean' | 'try-searching' = 'did-you-mean';

  const didYouMeanIndex = msg.indexOf("Did you mean: ");
  const trySearchingIndex = msg.indexOf("Try searching for: ");

  if (didYouMeanIndex !== -1) {
    mainMsg = msg.substring(0, didYouMeanIndex).trim();
    const rawSuggestion = msg.substring(didYouMeanIndex + "Did you mean: ".length).trim();
    suggestion = rawSuggestion.replace(/^['"]|['"]\??$/g, '').trim();
    suggestionType = 'did-you-mean';
  } else if (trySearchingIndex !== -1) {
    mainMsg = msg.substring(0, trySearchingIndex).trim();
    const rawSuggestion = msg.substring(trySearchingIndex + "Try searching for: ".length).trim();
    suggestion = rawSuggestion;
    suggestionType = 'try-searching';
  }

  return { mainMsg, suggestion, suggestionType };
};

const parseTrySearchingList = (sug: string) => {
  return sug.split(',').map(item => {
    let cleaned = item.trim().replace(/^['"]|['"]\??$/g, '');
    if (cleaned.startsWith("or ")) {
      cleaned = cleaned.substring(3).trim();
    }
    if (cleaned.endsWith('.')) {
      cleaned = cleaned.slice(0, -1);
    }
    return cleaned.replace(/^['"]|['"]$/g, '').trim();
  }).filter(x => x.length > 0);
};

const detectMetricsInQuery = (query: string, metrics: string[]): string[] => {
  if (!query) return [];
  const qLower = query.toLowerCase();
  const detected: string[] = [];

  metrics.forEach(m => {
    const cleanMetric = m.toLowerCase().replace(/_/g, ' ');
    if (qLower.includes(m.toLowerCase()) || qLower.includes(cleanMetric)) {
      if (!detected.includes(m)) {
        detected.push(m);
      }
      return;
    }
    const words = cleanMetric.split(' ').filter(w => w.length >= 4);
    if (words.length > 0) {
      const matchesKeyword = words.some(w => {
        const regex = new RegExp(`\\b${w}\\b`, 'i');
        return regex.test(qLower);
      });
      if (matchesKeyword && !detected.includes(m)) {
        detected.push(m);
      }
    }
  });

  return detected;
};

const detectStateInQuery = (query: string, regions: string[]): string | null => {
  if (!query) return null;
  const qLower = query.toLowerCase();
  for (const r of regions) {
    if (qLower.includes(r.toLowerCase())) {
      return r;
    }
  }
  return null;
};

const detectYearInQuery = (query: string): string | null => {
  if (!query) return null;
  const match = query.match(/\b((?:19|20)\d{2})\b/);
  return match ? match[1] : null;
};

let cleanFetch: typeof fetch | null = null;
function getCleanFetch(): typeof fetch {
  if (cleanFetch) return cleanFetch;
  if (typeof window === 'undefined') return fetch;
  try {
    const iframe = document.createElement('iframe');
    iframe.style.display = 'none';
    document.body.appendChild(iframe);
    cleanFetch = iframe.contentWindow?.fetch || window.fetch;
  } catch (e) {
    cleanFetch = window.fetch;
  }
  return cleanFetch || window.fetch;
}


export default function Home() {
  const [results, setResults] = useState<any>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recentQueries, setRecentQueries] = useState<{query: string, result: any}[]>([]);
  const [devMode, setDevMode] = useState(false);
  const [darkMode, setDarkMode] = useState(true);
  const [forceLLM, setForceLLM] = useState(false);
  const [cacheCatalog, setCacheCatalog] = useState<any[]>([]);
  const [lineageNodes, setLineageNodes] = useState<any[]>([]);

  // Navigation tab state (analytics vs projects switcher dashboard)
  const [activeTab, setActiveTab] = useState<'analytics' | 'projects'>('analytics');
  const [selectedDb, setSelectedDb] = useState<string>('default');
  const selectedDbRef = useRef(selectedDb);
  selectedDbRef.current = selectedDb;
  const [databases, setDatabases] = useState<string[]>([]);
  const [federatedCount, setFederatedCount] = useState(8);
  const [dbLabels, setDbLabels] = useState<Record<string, string>>({});
  const [ownedDatabases, setOwnedDatabases] = useState<string[]>([]);
  const [authUser, setAuthUser] = useState<{ id: number; email: string } | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [authEmail, setAuthEmail] = useState('');
  const [authPassword, setAuthPassword] = useState('');
  const [authError, setAuthError] = useState<string | null>(null);
  const [authBusy, setAuthBusy] = useState(false);

  const applyCatalog = (data: { databases?: string[]; federated_count?: number; labels?: Record<string, string>; owned?: string[] }) => {
    if (Array.isArray(data.databases)) setDatabases(data.databases);
    if (typeof data.federated_count === 'number') setFederatedCount(data.federated_count);
    if (data.labels) setDbLabels(data.labels);
    if (Array.isArray(data.owned)) setOwnedDatabases(data.owned);
  };

  // Project upload form state
  const [uploadProjectName, setUploadProjectName] = useState('');
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Metadata props
  const [metadataMetrics, setMetadataMetrics] = useState<string[]>([]);
  const [metadataDepts, setMetadataDepts] = useState<string[]>([]);
  const [metadataRegions, setMetadataRegions] = useState<string[]>([]);
  const [metadataYears, setMetadataYears] = useState<string[]>([]);
  const [metadataGroups, setMetadataGroups] = useState<{ title: string; items: string[] }[]>([]);
  const [suggestedQueries, setSuggestedQueries] = useState<string[]>([]);

  // Session & conversation state
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [conversationContext, setConversationContext] = useState<ConversationContext | null>(null);

  // Query override state
  const [queryOverride, setQueryOverride] = useState<string | null>(null);
  const [lastSubmittedQuery, setLastSubmittedQuery] = useState('');

  // Live KPIs shown in the sidebar as the user types
  const [liveKpis, setLiveKpis] = useState<Record<string, number> | null>(null);
  const [liveQuery, setLiveQuery] = useState('');
  const liveDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const recentTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const liveQueryRef = useRef('');
  const requestSeq = useRef(0);
  // Prevents live-debounce from re-fetching when loading a cached recent result
  const skipNextLiveQueryRef = useRef(false);

  // Related metrics that appear after 2s of no typing
  const [relatedMetrics, setRelatedMetrics] = useState<string[]>([]);
  const relatedMetricsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (window.localStorage.getItem('alphabot-theme') === 'light') setDarkMode(false);
  }, []);

  // Suppress uncaught AbortErrors in Next.js Dev Overlay
  useEffect(() => {
    const handleRejection = (e: PromiseRejectionEvent) => {
      const msg = e.reason?.message || '';
      const name = e.reason?.name || '';
      if (name === 'AbortError' || msg.includes('aborted') || msg.includes('abort') || name.includes('Abort')) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    };
    const handleError = (e: ErrorEvent) => {
      const msg = e.message || '';
      const name = e.error?.name || '';
      if (name === 'AbortError' || msg.includes('aborted') || msg.includes('abort') || name.includes('Abort')) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    };
    window.addEventListener('unhandledrejection', handleRejection, true);
    window.addEventListener('error', handleError, true);
    return () => {
      window.removeEventListener('unhandledrejection', handleRejection, true);
      window.removeEventListener('error', handleError, true);
    };
  }, []);

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then(async (res) => {
        if (!res.ok) {
          setAuthUser(null);
          return;
        }
        const data = await res.json();
        setAuthUser(data.user || null);
      })
      .catch(() => setAuthUser(null))
      .finally(() => setAuthChecked(true));
  }, []);

  // Load active projects/databases list once the user is signed in
  useEffect(() => {
    if (!authUser) return;
    let intervalId: any = null;

    const fetchDbs = () => {
      fetch('/api/databases', { credentials: 'include' })
        .then(res => res.json())
        .then(data => {
          if (data.databases || typeof data.federated_count === 'number') {
            applyCatalog(data);
            // Stop polling once we successfully get a response
            if (intervalId) {
              clearInterval(intervalId);
              intervalId = null;
            }
          }
        })
        .catch(() => {
          // Keep polling if there is a network error (e.g. backend booting up)
        });
    };

    fetchDbs();
    intervalId = setInterval(fetchDbs, 4000);

    return () => {
      if (intervalId) {
        clearInterval(intervalId);
      }
    };
  }, [authUser]);

  // A reload starts a blank question. The previous answer is not put back in the box.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/session', { method: 'POST', credentials: 'include' });
        const data = await res.json();
        if (cancelled || !data.session_id) return;
        localStorage.setItem('alphabot_session_id', data.session_id);
        setSessionId(data.session_id);
      } catch {
        if (cancelled) return;
        const fallback = `local-${Date.now()}`;
        localStorage.setItem('alphabot_session_id', fallback);
        setSessionId(fallback);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Load this database's metrics, years, groups, and starter questions
  useEffect(() => {
    if (!authUser) return;
    let cancelled = false;
    setMetadataMetrics([]);
    setMetadataDepts([]);
    setMetadataRegions([]);
    setMetadataYears([]);
    setMetadataGroups([]);
    setSuggestedQueries([]);
    const url = selectedDb && selectedDb !== 'default'
      ? `/api/metadata?plant=${encodeURIComponent(selectedDb)}`
      : '/api/metadata';
    fetch(url, { credentials: 'include' })
      .then(r => r.json())
      .then(data => {
        if (cancelled) return;
        const metrics = Array.isArray(data.metrics) ? data.metrics : [];
        const years = Array.isArray(data.years) ? data.years.map((year: unknown) => String(year)) : [];
        const cats = data.categoricals && typeof data.categoricals === 'object' ? data.categoricals : {};
        const groups = Object.entries(cats)
          .filter((entry): entry is [string, unknown[]] => Array.isArray(entry[1]) && entry[1].length > 0 && !/date|year|month|time/i.test(entry[0]))
          .slice(0, 3)
          .map(([name, values]) => ({
            title: name.replace(/_/g, ' '),
            items: values.slice(0, 12).map(value => String(value)),
          }));
        setMetadataMetrics(metrics);
        setMetadataYears(years);
        setMetadataGroups(groups);
        setMetadataDepts(groups[0]?.items || []);
        setMetadataRegions(groups[1]?.items || []);
        setSuggestedQueries(Array.isArray(data.suggestions) ? data.suggestions : []);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [selectedDb, authUser]);

  // Recent queries belong to the database that produced them
  useEffect(() => {
    setResults(null);
    setLiveKpis(null);
    setError(null);
    setConversationContext(null);
    setLiveQuery('');
    liveQueryRef.current = '';
    setLastSubmittedQuery('');
    setQueryOverride(null);
    const key = `alphabot_recents_${selectedDb}`;
    const saved = localStorage.getItem(key) || (selectedDb === 'default' ? localStorage.getItem('alphabot_recents') : null);
    if (saved) {
      try {
        setRecentQueries(JSON.parse(saved));
        return;
      } catch {}
    }
    setRecentQueries([]);
  }, [selectedDb]);

  // Fetch active cache catalog and session lineage when Dev Mode is active
  useEffect(() => {
    if (devMode && sessionId) {
      // Fetch active cache tables
      fetch(`/api/session/${sessionId}/cache`)
        .then(res => res.json())
        .then(data => {
          if (data.caches) {
            setCacheCatalog(data.caches);
          }
        })
        .catch(err => console.error("Error fetching cache catalog:", err));

      // Fetch session lineage
      fetch(`/api/session/${sessionId}/lineage`)
        .then(res => res.json())
        .then(data => {
          if (data.lineage) {
            setLineageNodes(data.lineage);
          }
        })
        .catch(err => console.error("Error fetching lineage nodes:", err));
    }
  }, [devMode, sessionId, results]);

  // After 2 seconds of no typing, populate relatedMetrics with non-detected metrics
  useEffect(() => {
    // Clear related metrics immediately when typing resumes
    setRelatedMetrics([]);
    if (relatedMetricsTimerRef.current) clearTimeout(relatedMetricsTimerRef.current);

    if (!liveQuery.trim() || metadataMetrics.length === 0) return;

    relatedMetricsTimerRef.current = setTimeout(() => {
      const detected = detectMetricsInQuery(liveQuery, metadataMetrics);
      const related = metadataMetrics
        .filter(m => !detected.includes(m))
        .slice(0, 3);
      setRelatedMetrics(related);
    }, 2000);

    return () => {
      if (relatedMetricsTimerRef.current) clearTimeout(relatedMetricsTimerRef.current);
    };
  }, [liveQuery, metadataMetrics]);

  // Whenever the result changes, sync KPIs from the latest full result
  useEffect(() => {
    if (results?.kpis) setLiveKpis(results.kpis);
  }, [results]);

  // Live Search: debounced 800ms — only fires after user genuinely pauses
  // (avoids firing expensive Ollama calls on every single keystroke)
  const handleLiveTyping = (q: string) => {
    liveQueryRef.current = q;
    setLiveQuery(q);
    setError(null); // Clear errors immediately as user types
    if (liveDebounceRef.current) clearTimeout(liveDebounceRef.current);
    if (recentTimerRef.current) {
      clearTimeout(recentTimerRef.current);
      recentTimerRef.current = null;
    }
    
    const trimmed = q.trim();
    if (!trimmed) {
      requestSeq.current += 1;
      setResults(null);
      setLiveKpis(null);
      setLastSubmittedQuery('');
      setConversationContext(null);
      setIsLoading(false);
      return;
    }

    // If a recent result was just loaded from cache, skip the full query round-trip
    if (skipNextLiveQueryRef.current) {
      skipNextLiveQueryRef.current = false;
      return;
    }
    
    // Original live execution mode: Debounce 800ms -> Full Query execution
    liveDebounceRef.current = setTimeout(async () => {
      handleQuery(trimmed);
    }, 800);
  };

  const handleQuery = async (query: string) => {
    const trimmedQuery = query.trim();
    if (!trimmedQuery) {
      requestSeq.current += 1;
      setResults(null);
      setLiveKpis(null);
      setLastSubmittedQuery('');
      setIsLoading(false);
      return;
    }
    // Prevent double execution from active typing debounces
    if (liveDebounceRef.current) {
      clearTimeout(liveDebounceRef.current);
      liveDebounceRef.current = null;
    }

    const seq = ++requestSeq.current;
    setIsLoading(true);
    setError(null);
    setLastSubmittedQuery(trimmedQuery);
    setQueryOverride(null);

    try {
      const response = await fetch('/api/query', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          raw_query: query, 
          blueprint: null,
          force_llm: forceLLM,
          session_id: sessionId,   // ← always send session ID
          selected_db: selectedDb, // ← pass the selected project database!
        }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || 'Query failed');
      }

      const data = await response.json();
      if (seq !== requestSeq.current || liveQueryRef.current.trim() !== trimmedQuery) return;
      setResults(data);
      if (data.kpis) setLiveKpis(data.kpis);
      // Update conversation context from the response
      if (data.conversation_context) {
        setConversationContext(data.conversation_context);
      }


      // Save to recent queries after 5 seconds on screen
      if (recentTimerRef.current) {
        clearTimeout(recentTimerRef.current);
      }
      const dbAtQuery = selectedDb;
      const storageKey = `alphabot_recents_${dbAtQuery}`;
      recentTimerRef.current = setTimeout(() => {
        let prev: {query: string, result: any}[] = [];
        try { prev = JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch {}
        const filtered = prev.filter(item => item.query.toLowerCase() !== query.toLowerCase());
        const updated = [{ query, result: data }, ...filtered].slice(0, 5);
        localStorage.setItem(storageKey, JSON.stringify(updated));
        if (selectedDbRef.current === dbAtQuery) setRecentQueries(updated);
      }, 5000);

    } catch (err) {
      if (seq !== requestSeq.current || liveQueryRef.current.trim() !== trimmedQuery) return;
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      if (seq === requestSeq.current) setIsLoading(false);
    }
  };

  const loadRecent = (item: {query: string, result: any}) => {
    // Immediately display cached result — no HTTP call needed
    liveQueryRef.current = item.query;
    skipNextLiveQueryRef.current = true;
    if (liveDebounceRef.current) {
      clearTimeout(liveDebounceRef.current);
      liveDebounceRef.current = null;
    }
    setResults(item.result);
    setLastSubmittedQuery(item.query);
    setLiveQuery(item.query);
    setQueryOverride(item.query);
    if (item.result?.kpis) setLiveKpis(item.result.kpis);
    if (item.result?.conversation_context) setConversationContext(item.result.conversation_context);
    setError(null);
  };

  const handleFollowUpClick = (q: string) => {
    liveQueryRef.current = q;
    setQueryOverride(q);
    setLiveQuery(q);
    handleQuery(q);
  };

  const handleClearSession = async () => {
    if (sessionId) {
      await clearSession(sessionId);
      // Re-create a fresh session
      const { getOrCreateSessionId: mkSession } = await import('@/lib/session');
      const newSid = await mkSession();
      setSessionId(newSid);
    }
    setConversationContext(null);
    setResults(null);
    setLiveKpis(null);
    setLastSubmittedQuery('');
    setLiveQuery('');
    liveQueryRef.current = '';
    setQueryOverride('');
    setError(null);
  };
  const handleProjectUpload = async () => {
    if (!uploadFile || !uploadProjectName.trim()) return;
    
    setIsUploading(true);
    setUploadError(null);
    
    const formData = new FormData();
    formData.append('file', uploadFile);
    formData.append('name', uploadProjectName);
    
    try {
      const res = await fetch('/api/upload', {
        method: 'POST',
        credentials: 'include',
        body: formData,
      });
      
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || 'Upload failed');
      }
      
      const data = await res.json();
      
      // Update databases state
      if (data.databases || typeof data.federated_count === 'number') {
        applyCatalog(data);
      }
      
      // Select the newly uploaded database
      setSelectedDb(data.project);
      
      // Clear form states
      setUploadFile(null);
      setUploadProjectName('');
      
      // Redirect back to analytics dashboard to query
      setActiveTab('analytics');
      
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setIsUploading(false);
    }
  };

  const [deletingDb, setDeletingDb] = useState<string | null>(null);

  const handleDeleteProject = async (dbName: string, e: React.MouseEvent) => {
    e.stopPropagation(); // prevent card click selecting the db
    if (!window.confirm(`Delete project "${dbName.replace(/_/g, ' ')}"? This cannot be undone.`)) return;

    setDeletingDb(dbName);
    try {
      const res = await fetch(`/api/project/${encodeURIComponent(dbName)}`, { method: 'DELETE', credentials: 'include' });
      const data = await res.json().catch(() => ({}));

      if (!res.ok) throw new Error(data.detail || 'Delete failed');

      // Update databases list from server response
      if (data.databases !== undefined || typeof data.federated_count === 'number') applyCatalog(data);

      // Switch to default if we just deleted the active db
      if (selectedDb === dbName) setSelectedDb('default');

    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to delete project');
    } finally {
      setDeletingDb(null);
    }
  };

  const submitAuth = async () => {
    setAuthBusy(true);
    setAuthError(null);
    try {
      const path = authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
      const res = await fetch(path, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: authEmail, password: authPassword }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not sign in');
      if (authMode === 'register') {
        const loginRes = await fetch('/api/auth/login', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: authEmail, password: authPassword }),
        });
        const loginData = await loginRes.json().catch(() => ({}));
        if (!loginRes.ok) throw new Error(typeof loginData.detail === 'string' ? loginData.detail : 'Account created, but sign-in failed');
        setAuthUser(loginData.user);
      } else {
        setAuthUser(data.user);
      }
    } catch (err) {
      setAuthError(err instanceof Error ? err.message : 'Could not sign in');
    } finally {
      setAuthBusy(false);
    }
  };

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
    setAuthUser(null);
    setResults(null);
    setSelectedDb('default');
  };

  if (!authChecked) {
    return <div className="min-h-screen bg-[#05070d]" />;
  }

  if (!authUser) {
    return (
      <LandingPage
        open={authOpen}
        mode={authMode}
        email={authEmail}
        password={authPassword}
        error={authError}
        busy={authBusy}
        onOpen={(next) => {
          setAuthMode(next);
          setAuthError(null);
          setAuthOpen(true);
        }}
        onClose={() => setAuthOpen(false)}
        onEmail={setAuthEmail}
        onPassword={setAuthPassword}
        onSubmit={submitAuth}
        onToggleMode={() => {
          setAuthMode(authMode === 'login' ? 'register' : 'login');
          setAuthError(null);
        }}
      />
    );
  }

  return (
    <div className={darkMode ? 'dark' : ''}>
      <div className="work-app relative h-screen flex flex-col bg-[#f4f6fa] dark:bg-slate-950 overflow-hidden text-slate-800 dark:text-slate-100">
        {darkMode && (
          <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0">
            <div className="absolute -left-16 -top-20 h-[460px] w-[520px] rounded-full blur-3xl" style={{ background: 'radial-gradient(circle, rgba(0, 140, 95, 0.28) 0%, transparent 68%)' }} />
            <div className="absolute -bottom-24 -right-10 h-[420px] w-[480px] rounded-full blur-3xl" style={{ background: 'radial-gradient(circle, rgba(70, 190, 195, 0.16) 0%, transparent 70%)' }} />
          </div>
        )}
        <header className="relative z-20 h-14 flex-shrink-0 bg-white/80 dark:bg-slate-950 backdrop-blur px-3 sm:px-5 flex items-center gap-2 sm:gap-3">
          <span className="flex shrink-0 items-center gap-2">
            <svg viewBox="0 0 40 40" className="h-7 w-7" fill="none" aria-hidden="true">
              <path d="M20 3L7 36h7.2l2.4-6.4h6.8L25.8 36H33L20 3z" fill="#6ee7b7" />
              <path d="M15.4 25.2L20 12.6l4.6 12.6h-9.2z" fill="#064e3b" />
            </svg>
            <span className="text-sm font-semibold tracking-tight text-slate-900 dark:text-white">Alpha<span className="text-emerald-600 dark:text-emerald-300">bot</span></span>
          </span>
          <nav className="flex items-center gap-1 rounded-full bg-slate-100/80 dark:bg-white/[0.06] p-1 text-sm">
            <button
              type="button"
              onClick={() => setActiveTab('analytics')}
              className={`px-3 py-1 rounded-full ${activeTab === 'analytics' ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-white' : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'}`}
            >
              Query
            </button>
            <button
              type="button"
              onClick={() => setActiveTab('projects')}
              className={`px-3 py-1 rounded-full ${activeTab === 'projects' ? 'bg-white text-slate-900 shadow-sm dark:bg-white/10 dark:text-white' : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'}`}
            >
              Databases
            </button>
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-2 text-sm text-slate-500 dark:text-slate-400 sm:gap-3">
            <span className="hidden shrink-0 rounded-full bg-sky-50 px-3 py-1 text-sky-900 dark:bg-emerald-950/50 dark:text-emerald-200 sm:inline">
              {selectedDb === 'default'
                ? `Default · ${federatedCount} databases`
                : (dbLabels[selectedDb] || selectedDb).replace(/_/g, ' ')}
            </span>
            <span className="hidden max-w-[160px] truncate text-xs md:inline">{authUser.email}</span>
            <button
              type="button"
              onClick={() => setDevMode(!devMode)}
              className={`shrink-0 whitespace-nowrap text-xs hover:text-slate-900 dark:hover:text-slate-100 ${devMode ? 'text-slate-900 dark:text-slate-100' : ''}`}
            >
              {devMode ? 'Dev on' : 'Dev'}
            </button>
            <button
              type="button"
              aria-pressed={darkMode}
              aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
              onClick={() => {
                setDarkMode((on) => {
                  const next = !on;
                  window.localStorage.setItem('alphabot-theme', next ? 'dark' : 'light');
                  return next;
                });
              }}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-slate-200 text-slate-600 hover:text-slate-900 dark:border-white/15 dark:text-slate-300 dark:hover:text-white"
            >
              {darkMode ? (
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <circle cx="12" cy="12" r="4" />
                  <path strokeLinecap="round" d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 14.5A8.5 8.5 0 1 1 9.5 3 7 7 0 0 0 21 14.5z" />
                </svg>
              )}
            </button>
            <button type="button" onClick={logout} className="shrink-0 whitespace-nowrap text-xs hover:text-slate-900 dark:hover:text-slate-100">
              Sign out
            </button>
          </div>
        </header>

        <div className="relative z-10 flex min-h-0 min-w-0 flex-1 flex-col">

          {/* Developer Mode Panel */}
          {devMode && (
            <div className="bg-white dark:bg-slate-900 border-b border-stone-300 dark:border-slate-800 px-5 py-3 max-h-[35vh] overflow-y-auto">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-6 flex-wrap">

                  <div>
                    <h3 className="text-[11px] font-medium text-stone-500 dark:text-slate-400 mb-1.5">Engine</h3>
                    <div className="flex items-center gap-4">
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="radio"
                          checked={!forceLLM}
                          onChange={() => setForceLLM(false)}
                          className="w-3.5 h-3.5"
                        />
                        <span className="text-sm text-stone-800 dark:text-slate-200">Hybrid</span>
                      </label>
                      <label className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="radio"
                          checked={forceLLM}
                          onChange={() => setForceLLM(true)}
                          className="w-3.5 h-3.5"
                        />
                        <span className="text-sm text-stone-800 dark:text-slate-200">Model only</span>
                      </label>
                    </div>
                  </div>

                  <div className="h-8 w-px bg-stone-200 dark:bg-slate-700 hidden sm:block" />

                  {results?.metadata?.iqp && (
                    <div className="hidden sm:flex flex-col gap-0.5">
                      <h3 className="text-[11px] font-medium text-stone-500 dark:text-slate-400">Last route</h3>
                      <span className="text-sm text-stone-800 dark:text-slate-200">
                        {results.metadata.iqp.cache_hit ? 'Local cache' : 'Database'}
                      </span>
                    </div>
                  )}
                </div>

                {results?.metadata && (
                  <div className="text-right flex-shrink-0">
                    <div className="text-[11px] text-stone-500 dark:text-slate-400">Last query</div>
                    <div className="text-sm text-stone-900 dark:text-slate-100">
                      {Math.round(results.metadata.backend_ms || 0)} ms
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Main Content */}
          <main className="relative z-10 flex min-h-0 flex-1 flex-col overflow-y-auto md:flex-row md:overflow-hidden">

            {activeTab === 'analytics' ? (
              <>
                <aside className="flex w-full flex-shrink-0 flex-col border-b border-slate-200 bg-white dark:border-white/10 dark:bg-[rgba(12,16,14,0.62)] dark:backdrop-blur-md md:h-full md:w-[320px] md:overflow-hidden md:border-b-0 md:border-r lg:w-[360px]">
                  <div className="px-5 py-5 md:min-h-0 md:flex-1 md:overflow-y-auto">
                <p className="mb-4 text-sm font-medium text-slate-900 dark:text-white">Ask Alphabot</p>
                    <QueryInterface
                      key={selectedDb}
                      selectedDb={selectedDb}
                      onSubmit={handleQuery}
                      isLoading={isLoading}
                      recentQueries={recentQueries}
                      onSelectRecent={loadRecent}
                      liveKpis={liveKpis}
                      onQueryChange={handleLiveTyping}
                      results={results}
                      conversationContext={conversationContext}
                      devMode={devMode}
                      onClearSession={handleClearSession}
                      queryOverride={queryOverride}
                      onClearOverride={() => setQueryOverride(null)}
                      metadataMetrics={metadataMetrics}
                      metadataDepts={metadataDepts}
                      metadataRegions={metadataRegions}
                      metadataYears={metadataYears}
                      metadataGroups={metadataGroups}
                      suggestedQueries={suggestedQueries}
                    />
                  </div>
                </aside>

                {/* Right panel — Results & Errors */}
                <div className="md:min-h-0 md:flex-1 md:overflow-y-auto">
                  {liveQuery.trim() && error ? (
                    <div className="h-full flex items-center justify-center p-8">
                      <div className="max-w-md w-full bg-white dark:bg-slate-900 rounded-2xl p-5">
                        <h3 className="text-sm font-medium text-stone-900 dark:text-slate-100 mb-2">The query did not run</h3>
                        {(() => {
                          const { mainMsg, suggestion, suggestionType } = parseErrorMessage(error);
                          return (
                            <>
                              <p className="text-xs text-red-600 dark:text-red-400 font-semibold leading-relaxed mb-4">{mainMsg}</p>
                              {suggestion && (
                                suggestionType === 'did-you-mean' ? (
                                  <div 
                                    onClick={() => handleFollowUpClick(suggestion)}
                                    className="mt-4 border border-stone-300 dark:border-slate-700 p-3 text-left cursor-pointer hover:bg-stone-50 dark:hover:bg-slate-800"
                                  >
                                    <p className="text-xs text-stone-500 dark:text-slate-400 mb-1">Did you mean</p>
                                    <p className="text-sm text-stone-900 dark:text-slate-100">{suggestion}</p>
                                  </div>
                                ) : (
                                  <div className="mt-4">
                                    <p className="text-xs text-stone-500 dark:text-slate-400 mb-2">Try</p>
                                    <div className="flex flex-col">
                                      {parseTrySearchingList(suggestion).map(item => (
                                        <button
                                          key={item}
                                          type="button"
                                          onClick={() => handleFollowUpClick(item)}
                                          className="px-0 py-1.5 text-left text-sm text-stone-800 dark:text-slate-200 hover:underline"
                                        >
                                          {item}
                                        </button>
                                      ))}
                                    </div>
                                  </div>
                                )
                              )}
                            </>
                          );
                        })()}
                      </div>
                    </div>
                  ) : (liveQuery.trim() && (isLoading || results?.results || results?.status === 'clarification_required' || results?.status === 'success' || results?.status === 'error')) ? (
                    <ResultsDisplay 
                      data={results} 
                      devMode={devMode} 
                      onFollowUpClick={handleFollowUpClick} 
                      darkMode={darkMode}
                      queryLabel={lastSubmittedQuery}
                      isLoading={isLoading}
                      activeTypingMetrics={detectMetricsInQuery(liveQuery, metadataMetrics)}
                      relatedMetrics={relatedMetrics}
                      metadataMetrics={metadataMetrics}
                      detectedState={detectStateInQuery(liveQuery, metadataRegions)}
                      detectedYear={detectYearInQuery(liveQuery)}
                      cacheCatalog={cacheCatalog}
                      lineageNodes={lineageNodes}
                    />
                  ) : (
                    <div className="p-6 space-y-4">
                      <div className="grid grid-cols-2 xl:grid-cols-4 gap-4">
                        {metadataMetrics.slice(0, 4).map((metric) => (
                          <div key={metric} className="work-glass min-h-[104px] rounded-2xl border border-slate-200/80 bg-white px-5 py-4 dark:border-white/10 dark:bg-slate-900">
                            <p className="text-xs text-slate-500 capitalize">{metric.replace(/_/g, ' ')}</p>
                            <p className="mt-3 text-2xl text-slate-300">—</p>
                          </div>
                        ))}
                      </div>
                      <p className="text-sm text-slate-500">Ask a question in the sidebar, then press Enter.</p>
                    </div>
                  )}
                </div>
              </>
            ) : (
              <div className="flex-1 overflow-y-auto px-8 py-8">
                <div className="mb-6 max-w-3xl">
                  <h2 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Databases</h2>
                  <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">Pick one to query. Shared databases are available to everyone. A file you upload stays on your account.</p>
                </div>

                <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px] gap-6 items-start">
                  <div className="space-y-6">
                    <section>
                      <h3 className="mb-3 text-sm font-semibold text-slate-800 dark:text-slate-100">Shared</h3>
                      <div className="space-y-3">
                        <button
                          type="button"
                          onClick={() => { setSelectedDb('default'); setActiveTab('analytics'); }}
                          className={`w-full rounded-2xl border bg-white px-5 py-4 text-left dark:bg-slate-900 ${selectedDb === 'default' ? 'border-sky-400 ring-2 ring-sky-100' : 'border-slate-200 hover:border-slate-300 dark:border-slate-700'}`}
                        >
                          <div className="flex items-start justify-between gap-4">
                            <div>
                              <p className="text-base font-semibold text-slate-900 dark:text-slate-50">Default databases</p>
                              <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">The original {federatedCount} plant databases, queried together.</p>
                            </div>
                            {selectedDb === 'default' && (
                              <span className="shrink-0 rounded-full bg-sky-100 px-2.5 py-1 text-xs font-medium text-sky-900">In use</span>
                            )}
                          </div>
                          <p className="mt-3 text-sm font-medium text-sky-800">Query these</p>
                        </button>
                        {databases.filter(db => db !== 'default' && !ownedDatabases.includes(db)).map(db => {
                          const label = dbLabels[db] || db.replace(/_/g, ' ');
                          const active = selectedDb === db;
                          return (
                            <button
                              key={db}
                              type="button"
                              onClick={() => { setSelectedDb(db); setActiveTab('analytics'); }}
                              className={`w-full rounded-2xl border bg-white px-5 py-4 text-left dark:bg-slate-900 ${active ? 'border-sky-400 ring-2 ring-sky-100' : 'border-slate-200 hover:border-slate-300 dark:border-slate-700'}`}
                            >
                              <div className="flex items-start justify-between gap-4">
                                <div>
                                  <p className="text-base font-semibold capitalize text-slate-900 dark:text-slate-50">{label}</p>
                                  <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Shared demo database. Questions use only this file.</p>
                                </div>
                                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${active ? 'bg-sky-100 text-sky-900' : 'bg-slate-100 text-slate-700'}`}>
                                  {active ? 'In use' : 'Shared'}
                                </span>
                              </div>
                              <p className="mt-3 text-sm font-medium text-sky-800">Query this</p>
                            </button>
                          );
                        })}
                      </div>
                    </section>

                    <section>
                      <h3 className="mb-3 text-sm font-semibold text-slate-800 dark:text-slate-100">Your uploads</h3>
                      {ownedDatabases.length === 0 ? (
                        <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-5 py-4 dark:border-slate-700 dark:bg-slate-900">
                          <p className="text-sm text-slate-700 dark:text-slate-200">No uploads yet. Add a CSV, Excel, or SQLite file on the right.</p>
                        </div>
                      ) : (
                        <div className="space-y-3">
                          {ownedDatabases.map(db => {
                            const label = dbLabels[db] || db.replace(/_/g, ' ');
                            const active = selectedDb === db;
                            return (
                              <div
                                key={db}
                                className={`rounded-2xl border bg-white px-5 py-4 dark:bg-slate-900 ${active ? 'border-sky-400 ring-2 ring-sky-100' : 'border-slate-200 dark:border-slate-700'}`}
                              >
                                <div className="flex items-start justify-between gap-4">
                                  <div>
                                    <p className="text-base font-semibold capitalize text-slate-900 dark:text-slate-50">{label}</p>
                                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">Only you can query or delete this file.</p>
                                  </div>
                                  <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${active ? 'bg-sky-100 text-sky-900' : 'bg-amber-100 text-amber-900'}`}>
                                    {active ? 'In use' : 'Yours'}
                                  </span>
                                </div>
                                <div className="mt-3 flex items-center gap-4">
                                  <button
                                    type="button"
                                    onClick={() => { setSelectedDb(db); setActiveTab('analytics'); }}
                                    className="text-sm font-medium text-sky-800"
                                  >
                                    Query this
                                  </button>
                                  <button
                                    type="button"
                                    title={`Delete ${label}`}
                                    disabled={deletingDb === db}
                                    onClick={(e) => handleDeleteProject(db, e)}
                                    className="text-sm font-medium text-slate-500 hover:text-red-700 disabled:opacity-50"
                                  >
                                    {deletingDb === db ? 'Deleting' : 'Delete'}
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </section>
                  </div>

                  <form
                    className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-900"
                    onSubmit={(e) => { e.preventDefault(); handleProjectUpload(); }}
                  >
                    <h3 className="text-base font-semibold text-slate-900 dark:text-slate-50">Add a database</h3>
                    <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">CSV, Excel, or a SQLite file that contains a metrics table.</p>
                    <label className="mt-4 block text-sm font-medium text-slate-800 dark:text-slate-100">
                      Name
                      <input
                        type="text"
                        placeholder="Sales Q4"
                        value={uploadProjectName}
                        onChange={(e) => setUploadProjectName(e.target.value)}
                        className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-normal text-slate-900 focus:outline-none focus:ring-2 focus:ring-sky-200 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                      />
                    </label>
                    <label className="mt-4 block text-sm font-medium text-slate-800 dark:text-slate-100">
                      File
                      <input
                        type="file"
                        accept=".csv,.xlsx,.xls,.db"
                        onChange={(e) => {
                          if (e.target.files && e.target.files.length > 0) {
                            const file = e.target.files[0];
                            setUploadFile(file);
                            if (!uploadProjectName.trim()) {
                              const base = file.name.replace(/\.[^/.]+$/, "");
                              const cleanedBase = base.replace(/[_\-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
                              setUploadProjectName(cleanedBase);
                            }
                          }
                        }}
                        className="mt-1.5 block w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-normal text-slate-700 file:mr-3 file:rounded-full file:border-0 file:bg-white file:px-3 file:py-1 file:text-sm file:font-medium file:text-slate-800"
                      />
                    </label>
                    <p className="mt-2 text-sm text-slate-600">{uploadFile ? uploadFile.name : 'No file chosen yet'}</p>
                    {uploadError && (
                      <p className="mt-2 text-sm text-red-700">{uploadError}</p>
                    )}
                    <button
                      type="submit"
                      disabled={isUploading || !uploadFile || !uploadProjectName.trim()}
                      className="mt-4 w-full rounded-full bg-slate-900 py-2.5 text-sm font-medium text-white disabled:bg-slate-200 disabled:text-slate-500 dark:bg-emerald-800 dark:text-emerald-50 dark:disabled:bg-slate-800 dark:disabled:text-slate-500"
                    >
                      {isUploading ? 'Uploading' : 'Upload'}
                    </button>
                  </form>
                </div>
              </div>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
