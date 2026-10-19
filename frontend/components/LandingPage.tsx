'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';

const PREVIEWS = [
  {
    query: 'total revenue',
    bars: [
      { label: 'Combined', width: '100%', value: '907.1M' },
    ],
  },
  {
    query: 'revenue by region',
    bars: [
      { label: 'North', width: '86%', value: '180' },
      { label: 'South', width: '34%', value: '50' },
    ],
  },
  {
    query: 'orders in 2024',
    bars: [
      { label: '2024', width: '72%', value: '150' },
      { label: '2023', width: '48%', value: '80' },
    ],
  },
];

type LandingPageProps = {
  open: boolean;
  mode: 'login' | 'register';
  email: string;
  password: string;
  error: string | null;
  busy: boolean;
  onOpen: (mode: 'login' | 'register') => void;
  onClose: () => void;
  onEmail: (value: string) => void;
  onPassword: (value: string) => void;
  onSubmit: () => void;
  onGoogle: (credential: string) => void;
  onToggleMode: () => void;
};

type GoogleCredentialResponse = { credential?: string };

declare global {
  interface Window {
    google?: {
      accounts?: {
        id?: {
          initialize: (config: { client_id: string; callback: (response: GoogleCredentialResponse) => void }) => void;
          renderButton: (parent: HTMLElement, options: Record<string, string | number>) => void;
        };
      };
    };
  }
}

const CHART_QUESTIONS = [
  {
    query: 'revenue by month',
    labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
    values: [46, 70, 54, 86, 62, 92],
  },
  {
    query: 'revenue over the year',
    labels: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
    values: [28, 48, 40, 66, 58, 82],
  },
  {
    query: 'orders by region',
    labels: ['North', 'South', 'East', 'West'],
    values: [82, 48, 66, 57],
  },
];

const NAV_LINKS = [
  { href: '#product', label: 'Product' },
  { href: '#how', label: 'How it works' },
  { href: '#charts', label: 'Charts' },
];

const FEATURES: Array<{ title: string; body: string; icon: ReactNode }> = [
  {
    title: 'Your files stay yours',
    body: 'What you upload lives on your account. Other people cannot open it or delete it.',
    icon: (
      <path strokeLinecap="round" strokeLinejoin="round" d="M7 11V8a5 5 0 0110 0v3M6 11h12v9H6z" />
    ),
  },
  {
    title: 'It reads the file you picked',
    body: 'A question uses that database’s own columns, so a sales sheet and a plant file stay separate.',
    icon: (
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16M9 6v12M15 6v12" />
    ),
  },
  {
    title: 'Try it before you upload',
    body: 'The eight plants are ready together. You can also open one database on its own.',
    icon: (
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 5.5v13l10-6.5-10-6.5z" />
    ),
  },
];

const STEPS = [
  ['Create an account', 'Continue with Google, or sign up with email and a password of at least 8 characters.'],
  ['Upload your data', 'Add a CSV, spreadsheet, or SQLite database from the projects view.'],
  ['Ask the question', 'Type it the way you would say it, then read the result for that database.'],
];

function linePoints(values: number[]) {
  const width = 360;
  const height = 168;
  return values.map((value, index) => {
    const x = ((index + 0.5) / values.length) * width;
    const y = height - 8 - (value / 100) * (height - 20);
    return { x, y };
  });
}

function GoogleSignIn({ onGoogle, busy }: { onGoogle: (credential: string) => void; busy: boolean }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const clientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || '';

  useEffect(() => {
    if (!clientId || !hostRef.current) return;
    let cancelled = false;
    const render = () => {
      const host = hostRef.current;
      const googleId = window.google?.accounts?.id;
      if (cancelled || !host || !googleId) return;
      googleId.initialize({
        client_id: clientId,
        callback: (response) => {
          if (response.credential) onGoogle(response.credential);
        },
      });
      host.replaceChildren();
      googleId.renderButton(host, {
        type: 'standard',
        theme: 'outline',
        size: 'large',
        text: 'continue_with',
        shape: 'pill',
        width: 340,
        logo_alignment: 'left',
      });
    };
    if (window.google?.accounts?.id) {
      render();
      return () => { cancelled = true; };
    }
    let script = document.querySelector<HTMLScriptElement>('script[data-alphabot-gsi]');
    if (!script) {
      script = document.createElement('script');
      script.src = 'https://accounts.google.com/gsi/client';
      script.async = true;
      script.dataset.alphabotGsi = '1';
      document.head.appendChild(script);
    }
    script.addEventListener('load', render);
    return () => {
      cancelled = true;
      script?.removeEventListener('load', render);
    };
  }, [clientId, onGoogle]);

  if (!clientId) {
    return (
      <button
        type="button"
        disabled={busy}
        onClick={() => onGoogle('')}
        className="ab-btn ab-btn-ghost mt-6 w-full"
      >
        Continue with Google
      </button>
    );
  }

  return <div ref={hostRef} className="mt-6 flex min-h-11 justify-center" />;
}

function TrafficDots() {
  return (
    <div className="flex items-center gap-1.5" aria-hidden="true">
      <span className="h-2.5 w-2.5 rounded-full bg-rose-400/90" />
      <span className="h-2.5 w-2.5 rounded-full bg-amber-300/90" />
      <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/90" />
    </div>
  );
}

function QuestionPills({
  items,
  active,
  onSelect,
  label,
}: {
  items: string[];
  active: number;
  onSelect: (index: number) => void;
  label: string;
}) {
  return (
    <div className="flex flex-wrap gap-2" role="group" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item}
          type="button"
          onClick={() => onSelect(index)}
          aria-pressed={index === active}
          className={`rounded-full border px-3 py-1.5 text-[13px] font-medium transition-colors ${
            index === active
              ? 'border-[rgba(99,240,192,0.45)] bg-[rgba(99,240,192,0.12)] text-[#d9fbee]'
              : 'border-[rgba(220,255,245,0.12)] text-[#b2c2c8] hover:border-[rgba(220,255,245,0.24)] hover:text-white'
          }`}
        >
          {item}
        </button>
      ))}
    </div>
  );
}

function AnswerCharts({
  index,
  onSelect,
}: {
  index: number;
  onSelect: (index: number) => void;
}) {
  const { query, labels, values } = CHART_QUESTIONS[index];
  const points = linePoints(values);
  const line = points.map((point, i) => `${i === 0 ? 'M' : 'L'}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
  const area = points.length
    ? `${line} L${points[points.length - 1].x.toFixed(1)} 168 L${points[0].x.toFixed(1)} 168 Z`
    : '';

  const axis = (
    <div className="absolute inset-x-0 bottom-0 flex h-7 items-end gap-3 px-1">
      {labels.map((label) => (
        <span key={label} className="flex-1 text-center text-[12px] font-medium text-[#a9bac1]">{label}</span>
      ))}
    </div>
  );

  const guides = (
    <div className="absolute inset-x-0 top-0 bottom-7 flex flex-col justify-between">
      {[0, 1, 2, 3].map((lineIndex) => (
        <div key={lineIndex} className="border-t border-[rgba(220,255,245,0.08)]" />
      ))}
    </div>
  );

  return (
    <div className="relative mt-10">
      <div className="ab-glow-behind" aria-hidden="true" />
      <div className="ab-glass-strong relative p-5 sm:p-7 md:p-8">
        <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <TrafficDots />
            <p className="text-[15px] font-semibold text-[#f5f8f7]">Sample answer</p>
            <span className="ab-pill px-2.5 py-0.5 text-[12px] font-medium">Sample data</span>
          </div>
          <QuestionPills
            items={CHART_QUESTIONS.map((item) => item.query)}
            active={index}
            onSelect={onSelect}
            label="Sample chart questions"
          />
        </div>

        <div className="mt-6">
          <p className="ab-label">Question</p>
          <p className="mt-2 flex h-7 items-center text-[17px] font-medium text-[#f5f8f7]">
            <span key={query}>{query}</span>
            <span className="landing-motion ml-1 inline-block h-5 w-px bg-[#63f0c0] animate-[landing-caret_1s_steps(1)_infinite]" />
          </p>
        </div>

        <div className="mt-6 grid gap-5 md:grid-cols-2" key={query}>
          <figure className="ab-inset p-5">
            <figcaption className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-[#e6eeec]">Bar chart</span>
              <span className="ab-label">{labels.length} groups</span>
            </figcaption>
            <div className="relative mt-4 h-52">
              {guides}
              <div className="absolute inset-x-0 top-0 bottom-7 flex items-end gap-3 px-1">
                {values.map((value, i) => (
                  <div key={labels[i]} className="flex h-full flex-1 items-end">
                    <div
                      className="landing-motion w-full origin-bottom rounded-t-md bg-gradient-to-t from-[#0f8f68] to-[#63f0c0]"
                      style={{
                        height: `${value}%`,
                        animation: `landing-grow 0.85s cubic-bezier(0.22, 0.7, 0.2, 1) ${i * 0.07}s both`,
                      }}
                    />
                  </div>
                ))}
              </div>
              {axis}
            </div>
          </figure>

          <figure className="ab-inset p-5">
            <figcaption className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-[#e6eeec]">Line chart</span>
              <span className="ab-label">Same answer</span>
            </figcaption>
            <div className="relative mt-4 h-52">
              {guides}
              <div className="absolute inset-x-0 top-0 bottom-7">
                <svg viewBox="0 0 360 168" preserveAspectRatio="none" className="h-full w-full" aria-hidden="true">
                  <defs>
                    <linearGradient id="ab-area" x1="0" x2="0" y1="0" y2="1">
                      <stop offset="0%" stopColor="rgba(99,240,192,0.32)" />
                      <stop offset="100%" stopColor="rgba(99,240,192,0)" />
                    </linearGradient>
                  </defs>
                  <path d={area} fill="url(#ab-area)" className="landing-motion" style={{ animation: 'landing-in 0.9s ease both' }} />
                  <path
                    d={line}
                    fill="none"
                    stroke="#63f0c0"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                    className="landing-motion"
                    style={{ animation: 'landing-in 0.6s ease both' }}
                  />
                </svg>
                {points.map((point, i) => (
                  <span
                    key={labels[i]}
                    className="landing-motion absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[#63f0c0] bg-[#0b1210]"
                    style={{
                      left: `${((i + 0.5) / values.length) * 100}%`,
                      top: `${(point.y / 168) * 100}%`,
                      animation: `landing-in 0.35s ease ${0.85 + i * 0.08}s both`,
                    }}
                  />
                ))}
              </div>
              {axis}
            </div>
          </figure>
        </div>
      </div>
    </div>
  );
}

const BACKGROUND_GRAPHS: Array<{
  top: string;
  left?: string;
  right?: string;
  delay: string;
  duration: string;
  rotate: number;
  width: number;
  d: string;
  dots: Array<[number, number]>;
}> = [
  { top: '7%', left: '3%', delay: '0s', duration: '13s', rotate: 8, width: 150, d: 'M16 28 L42 50 L70 36 L98 58 L136 28', dots: [[42, 50], [98, 58], [136, 28]] },
  { top: '13%', right: '3%', delay: '-3s', duration: '12s', rotate: -10, width: 168, d: 'M16 62 L40 50 L62 54 L88 32 L114 40 L144 22', dots: [[40, 50], [88, 32], [144, 22]] },
  { top: '47%', left: '2%', delay: '-5s', duration: '14s', rotate: 6, width: 140, d: 'M16 52 L38 34 L60 46 L84 28 L108 42 L132 26', dots: [[38, 34], [84, 28], [132, 26]] },
  { top: '70%', right: '2%', delay: '-2s', duration: '12s', rotate: -6, width: 156, d: 'M14 44 C38 22 58 64 86 38 S124 24 148 48', dots: [[86, 38], [148, 48]] },
];

function BackgroundGraphs() {
  const nodes = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    const rests = new Map<HTMLDivElement, { x: number; y: number }>();
    const measure = () => {
      nodes.current.forEach((node) => {
        if (!node) return;
        const current = node.style.transform;
        node.style.transition = 'none';
        node.style.transform = 'none';
        const rect = node.getBoundingClientRect();
        node.style.transform = current;
        node.style.transition = '';
        rests.set(node, { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 });
      });
    };
    const onMove = (event: MouseEvent) => {
      const reach = 200;
      nodes.current.forEach((node) => {
        if (!node) return;
        const rest = rests.get(node);
        if (!rest) return;
        const dx = rest.x - event.clientX;
        const dy = rest.y - event.clientY;
        const dist = Math.hypot(dx, dy);
        if (dist < reach && dist > 0) {
          const strength = (1 - dist / reach) * 80;
          node.style.transform = `translate(${(dx / dist) * strength}px, ${(dy / dist) * strength}px)`;
        } else {
          node.style.transform = 'translate(0px, 0px)';
        }
      });
    };
    measure();
    window.addEventListener('mousemove', onMove);
    window.addEventListener('scroll', measure, { passive: true });
    window.addEventListener('resize', measure);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('scroll', measure);
      window.removeEventListener('resize', measure);
    };
  }, []);

  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 z-0 hidden xl:block">
      {BACKGROUND_GRAPHS.map((graph, index) => (
        <div
          key={`${graph.top}-${graph.left ?? graph.right}`}
          ref={(node) => { nodes.current[index] = node; }}
          className="bg-graph-shift"
          style={{ top: graph.top, left: graph.left, right: graph.right }}
        >
          <div className="bg-graph" style={{ animationDelay: graph.delay, animationDuration: graph.duration }}>
            <svg
              width={graph.width}
              height="92"
              viewBox="0 0 160 92"
              fill="none"
              style={{ transform: `rotate(${graph.rotate}deg)`, opacity: 0.55 }}
            >
              <path d={graph.d} stroke="rgba(99, 240, 192, 0.8)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
              {graph.dots.map(([cx, cy]) => (
                <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r="2.2" fill="rgba(204, 251, 236, 0.95)" />
              ))}
            </svg>
          </div>
        </div>
      ))}
    </div>
  );
}

function Mark({ className = 'h-10 w-10' }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" className={className} fill="none" aria-hidden="true">
      <path d="M20 3L7 36h7.2l2.4-6.4h6.8L25.8 36H33L20 3z" fill="#6ee7b7" />
      <path d="M15.4 25.2L20 12.6l4.6 12.6h-9.2z" fill="#064e3b" />
    </svg>
  );
}

function Wordmark({ size = 'lg' }: { size?: 'lg' | 'sm' }) {
  return (
    <span className={`brand font-semibold tracking-[-0.03em] text-white ${size === 'lg' ? 'text-[1.5rem]' : 'text-[1.25rem]'}`}>
      Alpha<span className="text-[#63f0c0]">bot</span>
    </span>
  );
}

export function LandingPage({
  open,
  mode,
  email,
  password,
  error,
  busy,
  onOpen,
  onClose,
  onEmail,
  onPassword,
  onSubmit,
  onGoogle,
}: LandingPageProps) {
  const [previewIndex, setPreviewIndex] = useState(0);
  const [chartIndex, setChartIndex] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const preview = PREVIEWS[previewIndex];

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    const timer = window.setInterval(() => {
      setPreviewIndex((current) => (current + 1) % PREVIEWS.length);
    }, 3200);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    const onResize = () => {
      if (window.innerWidth >= 768) setMenuOpen(false);
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onResize);
    };
  }, [menuOpen]);

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    const timer = window.setInterval(() => {
      setChartIndex((current) => (current + 1) % CHART_QUESTIONS.length);
    }, 3600);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return;
    const nodes = Array.from(document.querySelectorAll('[data-reveal]'));
    const waiting = nodes.filter((node) => node.getBoundingClientRect().top > window.innerHeight * 0.88);
    waiting.forEach((node) => node.classList.add('reveal-wait'));
    const show = (node: Element) => {
      if (node.getBoundingClientRect().top < window.innerHeight * 0.86) {
        node.classList.add('is-in');
      }
    };
    const onScroll = () => waiting.forEach(show);
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  const openAuth = (next: 'login' | 'register') => {
    setMenuOpen(false);
    onOpen(next);
  };

  return (
    <div className="landing">
      <div className="ab-bg" aria-hidden="true">
        <div className="ab-grid" />
        <div className="ab-glow ab-glow-hero landing-motion" />
        <div className="ab-glow ab-glow-teal" />
        <div className="ab-glow ab-glow-left" />
        <div className="ab-glow ab-glow-low" />
      </div>
      <BackgroundGraphs />

      <header className="fixed inset-x-0 top-0 z-30 pt-3">
        <div className="ab-container">
          <div className="ab-nav flex h-[72px] items-center justify-between px-4 sm:px-5">
            <a href="#top" className="flex items-center gap-2.5 rounded-lg" aria-label="Alphabot home">
              <Mark className="h-9 w-9" />
              <Wordmark />
            </a>
            <nav aria-label="Main" className="hidden items-center gap-8 text-[15px] font-medium md:flex">
              {NAV_LINKS.map((link) => (
                <a key={link.href} href={link.href} className="ab-link rounded-md">{link.label}</a>
              ))}
            </nav>
            <div className="flex items-center gap-2">
              <button type="button" onClick={() => openAuth('login')} className="ab-link hidden h-10 rounded-lg px-3 text-[15px] font-semibold md:inline-flex md:items-center">
                Sign in
              </button>
              <button type="button" onClick={() => openAuth('register')} className="ab-btn ab-btn-primary ab-btn-sm hidden sm:inline-flex">
                Sign up
              </button>
              <button
                type="button"
                className="ab-btn ab-btn-ghost ab-btn-sm w-10 px-0 md:hidden"
                aria-label={menuOpen ? 'Close menu' : 'Open menu'}
                aria-expanded={menuOpen}
                aria-controls="mobile-menu"
                onClick={() => setMenuOpen((value) => !value)}
              >
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                  {menuOpen
                    ? <path strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
                    : <path strokeLinecap="round" d="M4 7h16M4 12h16M4 17h16" />}
                </svg>
              </button>
            </div>
          </div>
          {menuOpen && (
            <div id="mobile-menu" className="ab-nav mt-2 p-3 md:hidden">
              <nav aria-label="Mobile" className="flex flex-col">
                {NAV_LINKS.map((link) => (
                  <a
                    key={link.href}
                    href={link.href}
                    onClick={() => setMenuOpen(false)}
                    className="ab-link rounded-lg px-3 py-3 text-[16px] font-medium hover:bg-white/5"
                  >
                    {link.label}
                  </a>
                ))}
              </nav>
              <div className="mt-2 grid grid-cols-2 gap-2 border-t border-[rgba(220,255,245,0.1)] pt-3">
                <button type="button" onClick={() => openAuth('login')} className="ab-btn ab-btn-ghost">Sign in</button>
                <button type="button" onClick={() => openAuth('register')} className="ab-btn ab-btn-primary">Sign up</button>
              </div>
            </div>
          )}
        </div>
      </header>

      <main id="top" className="relative z-10 scroll-mt-28">
        <section className="ab-container pt-[144px] text-center sm:pt-[156px]">
          <h1 className="ab-display landing-motion mx-auto max-w-[1000px] text-[#f5f8f7] animate-[landing-rise_0.8s_ease_both]">
            <span className="sm:block">Ask your databases </span>
            <span className="sm:block">in <span className="ab-highlight">plain English</span><span className="text-[#63f0c0]">.</span></span>
          </h1>
          <p className="ab-lead landing-motion mx-auto mt-6 max-w-[680px] animate-[landing-rise_0.9s_ease_0.08s_both]">
            Sign in, upload a spreadsheet, and ask about it in your own words.
            If you want to look around first, eight plant databases are already there.
          </p>
          <div className="landing-motion mt-9 flex flex-col items-stretch justify-center gap-3 sm:flex-row sm:items-center animate-[landing-rise_0.9s_ease_0.16s_both]">
            <button type="button" onClick={() => openAuth('register')} className="ab-btn ab-btn-primary px-6">
              Get started
            </button>
            <button type="button" onClick={() => openAuth('login')} className="ab-btn ab-btn-ghost px-6">
              Sign in
            </button>
          </div>
        </section>

        <section aria-label="Sample query preview" className="ab-container relative mt-10 pb-6 sm:mt-12">
          <div className="relative mx-auto max-w-[1000px]">
            <div className="ab-glow-behind" aria-hidden="true" />
            <div className="ab-glass-strong landing-motion relative overflow-hidden animate-[landing-rise_0.9s_ease_0.24s_both]">
              <div className="flex items-center justify-between gap-3 border-b border-[rgba(220,255,245,0.09)] px-5 py-4 sm:px-7">
                <div className="flex items-center gap-3">
                  <TrafficDots />
                  <p className="text-[15px] font-semibold text-[#f5f8f7]">Sample preview</p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="ab-pill hidden px-2.5 py-0.5 text-[12px] font-medium sm:inline-flex">Sample data</span>
                  <span className="text-[13px] font-medium text-[#b2c2c8]">Eight plant databases</span>
                </div>
              </div>

              <div className="grid gap-8 p-5 sm:p-7 md:grid-cols-[1fr_1.15fr] md:gap-10 md:p-8">
                <div>
                  <p className="ab-label">Question</p>
                  <div className="ab-inset mt-3 flex h-14 items-center gap-3 px-4">
                    <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] shrink-0 text-[#63f0c0]" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                      <circle cx="11" cy="11" r="6.5" />
                      <path strokeLinecap="round" d="M16 16l4 4" />
                    </svg>
                    <p className="flex items-center text-[16px] font-medium text-[#f5f8f7]">
                      <span key={preview.query}>{preview.query}</span>
                      <span className="landing-motion ml-1 inline-block h-5 w-px bg-[#63f0c0] animate-[landing-caret_1s_steps(1)_infinite]" />
                    </p>
                  </div>
                  <p className="ab-label mt-6">Try another</p>
                  <div className="mt-3">
                    <QuestionPills
                      items={PREVIEWS.map((item) => item.query)}
                      active={previewIndex}
                      onSelect={setPreviewIndex}
                      label="Sample questions"
                    />
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <p className="ab-label">Answer</p>
                    <p className="ab-label">{preview.bars.length === 1 ? '1 row' : `${preview.bars.length} rows`}</p>
                  </div>
                  <div className="mt-3 min-h-[148px] space-y-5" key={preview.query}>
                    {preview.bars.map((bar, index) => (
                      <div key={bar.label}>
                        <div className="mb-2.5 flex items-end justify-between gap-4">
                          <span className="text-[15px] font-medium text-[#d3dee1]">{bar.label}</span>
                          <span className="ab-metric text-[#f5f8f7]">{bar.value}</span>
                        </div>
                        <div className="h-2 overflow-hidden rounded-full bg-[rgba(220,255,245,0.08)]">
                          <div
                            className="landing-motion h-full origin-left rounded-full bg-gradient-to-r from-[#10b981] to-[#63f0c0]"
                            style={{ width: bar.width, animation: `landing-bar 0.8s ease ${index * 0.08}s both` }}
                          />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </main>

      <section id="charts" data-reveal className="ab-section">
        <div className="ab-container">
          <div className="max-w-[640px]">
            <p className="ab-eyebrow">Charts</p>
            <h2 className="ab-h2 mt-4 text-[#f5f8f7]">See an answer as a graph</h2>
            <p className="ab-lead mt-4">The question changes, and the chart changes with it.</p>
          </div>
          <AnswerCharts index={chartIndex} onSelect={setChartIndex} />
        </div>
      </section>

      <section id="product" data-reveal className="ab-section pt-0">
        <div className="ab-container">
          <div className="max-w-[640px]">
            <p className="ab-eyebrow">Product</p>
            <h2 className="ab-h2 mt-4 text-[#f5f8f7]">Your data, answered on its own terms.</h2>
          </div>
          <div className="mt-10 grid gap-5 md:grid-cols-3">
            {FEATURES.map((feature, index) => (
              <article
                key={feature.title}
                data-reveal
                style={{ transitionDelay: `${index * 90}ms` }}
                className="ab-glass ab-hover p-6 sm:p-7"
              >
                <div className="flex items-center justify-between">
                  <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-[rgba(99,240,192,0.24)] bg-[rgba(99,240,192,0.1)] text-[#63f0c0]">
                    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                      {feature.icon}
                    </svg>
                  </span>
                  <span className="brand text-[15px] font-medium tabular-nums text-[#8b9da5]">0{index + 1}</span>
                </div>
                <h3 className="ab-h3 mt-6 text-[#f5f8f7]">{feature.title}</h3>
                <p className="ab-body mt-2.5">{feature.body}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section id="how" data-reveal className="ab-section pt-0">
        <div className="ab-container grid items-start gap-10 lg:grid-cols-[0.85fr_1.15fr] lg:gap-16">
          <div className="lg:sticky lg:top-32">
            <p className="ab-eyebrow">How it works</p>
            <h2 className="ab-h2 mt-4 max-w-[12ch] text-[#f5f8f7]">From a file to an answer.</h2>
            <button type="button" onClick={() => openAuth('register')} className="ab-btn ab-btn-ghost mt-8">
              Get started
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </button>
          </div>
          <ol className="ab-glass relative p-6 sm:p-8">
            {STEPS.map(([title, body], index) => (
              <li key={title} className="relative grid grid-cols-[44px_1fr] gap-5 pb-8 last:pb-0">
                {index < STEPS.length - 1 && (
                  <span aria-hidden="true" className="absolute left-[21px] top-12 bottom-1 w-px bg-gradient-to-b from-[rgba(99,240,192,0.4)] to-[rgba(99,240,192,0.06)]" />
                )}
                <span className="brand flex h-11 w-11 items-center justify-center rounded-full border border-[rgba(99,240,192,0.35)] bg-[rgba(99,240,192,0.08)] text-[16px] font-semibold text-[#63f0c0]">
                  {index + 1}
                </span>
                <div className="pt-1.5">
                  <h3 className="ab-h3 text-[#f5f8f7]">{title}</h3>
                  <p className="ab-body mt-1.5">{body}</p>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section data-reveal className="ab-section pt-0">
        <div className="ab-container">
          <div className="relative">
            <div className="ab-glow-behind" aria-hidden="true" />
            <div className="ab-glass-strong relative grid items-center gap-8 overflow-hidden p-7 sm:p-10 md:grid-cols-[1.35fr_0.65fr] md:p-12">
              <div>
                <h2 className="ab-h2 max-w-[17ch] text-[#f5f8f7]">Start with the demo, or bring a file of your own.</h2>
                <p className="ab-lead mt-4 max-w-[52ch]">
                  The eight plant databases are ready to query. Anything you upload stays on your account.
                </p>
                <div className="mt-8 flex flex-col gap-3 sm:flex-row">
                  <button type="button" onClick={() => openAuth('register')} className="ab-btn ab-btn-primary px-6">
                    Sign up
                  </button>
                  <button type="button" onClick={() => openAuth('login')} className="ab-btn ab-btn-ghost px-6">
                    Sign in
                  </button>
                </div>
              </div>
              <svg viewBox="0 0 260 160" className="hidden h-auto w-full md:block" fill="none" aria-hidden="true">
                {[40, 80, 120].map((y) => (
                  <line key={y} x1="0" x2="260" y1={y} y2={y} stroke="rgba(220,255,245,0.07)" />
                ))}
                <path d="M8 128 L52 104 L92 112 L136 72 L178 82 L222 40 L252 30" stroke="rgba(99,240,192,0.85)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M8 128 L52 104 L92 112 L136 72 L178 82 L222 40 L252 30 L252 160 L8 160 Z" fill="rgba(99,240,192,0.07)" />
                {[[52, 104], [136, 72], [222, 40]].map(([cx, cy]) => (
                  <circle key={cx} cx={cx} cy={cy} r="4" fill="#0b1210" stroke="#63f0c0" strokeWidth="2" />
                ))}
              </svg>
            </div>
          </div>
        </div>
      </section>

      <footer className="relative z-10 border-t border-[rgba(220,255,245,0.08)] bg-[rgba(5,9,8,0.55)]">
        <div className="ab-container py-12">
          <div className="flex flex-col gap-8 md:flex-row md:items-center md:justify-between">
            <div>
              <a href="#top" className="inline-flex items-center gap-2 rounded-lg">
                <Mark className="h-8 w-8" />
                <Wordmark size="sm" />
              </a>
              <p className="mt-3 text-[15px] text-[#b2c2c8]">Ask a database in plain English.</p>
            </div>
            <nav aria-label="Footer" className="flex flex-wrap gap-x-7 gap-y-3 text-[15px] font-medium">
              {NAV_LINKS.map((link) => (
                <a key={link.href} href={link.href} className="ab-link rounded-md">{link.label}</a>
              ))}
              <button type="button" onClick={() => openAuth('login')} className="ab-link rounded-md">Sign in</button>
            </nav>
          </div>
          <p className="mt-10 text-[13px] text-[#8b9da5]">© 2026 Alphabot</p>
        </div>
      </footer>

      {open && (
        <div className="fixed inset-0 z-50 flex items-end justify-center px-4 py-6 sm:items-center" role="presentation">
          <button
            type="button"
            aria-label="Close"
            className="absolute inset-0 bg-black/70 backdrop-blur-sm"
            onClick={onClose}
          />
          <form
            role="dialog"
            aria-modal="true"
            aria-labelledby="auth-title"
            className="ab-glass-strong landing-motion relative w-full max-w-[420px] bg-[rgba(12,20,18,0.94)] p-7 animate-[landing-panel_0.28s_ease_both]"
            onSubmit={(event) => {
              event.preventDefault();
              onSubmit();
            }}
          >
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="absolute right-4 top-4 flex h-9 w-9 items-center justify-center rounded-full text-[#b2c2c8] hover:bg-white/5 hover:text-white"
            >
              <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                <path strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
            <Mark className="h-8 w-8" />
            <div className="mt-5 inline-flex rounded-full border border-[rgba(220,255,245,0.1)] bg-white/[0.04] p-1 text-[14px] font-medium">
              <button
                type="button"
                onClick={() => onOpen('login')}
                aria-pressed={mode === 'login'}
                className={`rounded-full px-3.5 py-1.5 ${mode === 'login' ? 'bg-[#f5f8f7] text-[#07110d]' : 'text-[#b2c2c8] hover:text-white'}`}
              >
                Sign in
              </button>
              <button
                type="button"
                onClick={() => onOpen('register')}
                aria-pressed={mode === 'register'}
                className={`rounded-full px-3.5 py-1.5 ${mode === 'register' ? 'bg-[#f5f8f7] text-[#07110d]' : 'text-[#b2c2c8] hover:text-white'}`}
              >
                Sign up
              </button>
            </div>
            <h2 id="auth-title" className="mt-5 text-[26px] font-semibold tracking-[-0.02em] text-[#f5f8f7]">
              {mode === 'login' ? 'Welcome back' : 'Create your account'}
            </h2>
            <p className="ab-body mt-2 text-[15px]">
              {mode === 'login'
                ? 'Use your Gmail account, or the email and password for this site.'
                : 'Use your Gmail account, or an email and a password of at least 8 characters.'}
            </p>
            <GoogleSignIn onGoogle={onGoogle} busy={busy} />
            <div className="mt-5 flex items-center gap-3 text-[12px] uppercase tracking-[0.14em] text-[#8b9da5]">
              <span className="h-px flex-1 bg-[rgba(220,255,245,0.12)]" />
              or
              <span className="h-px flex-1 bg-[rgba(220,255,245,0.12)]" />
            </div>
            <label className="mt-5 block text-[14px] font-medium text-[#d3dee1]">
              Email
              <input
                type="email"
                required
                autoFocus
                value={email}
                onChange={(event) => onEmail(event.target.value)}
                placeholder="you@company.com"
                className="ab-input mt-2"
              />
            </label>
            <label className="mt-4 block text-[14px] font-medium text-[#d3dee1]">
              Password
              <input
                type="password"
                required
                minLength={8}
                value={password}
                onChange={(event) => onPassword(event.target.value)}
                placeholder="At least 8 characters"
                className="ab-input mt-2"
              />
            </label>
            {error && <p role="alert" className="mt-4 rounded-xl bg-red-500/10 px-3 py-2 text-[14px] text-red-200">{error}</p>}
            <button
              type="submit"
              disabled={busy}
              className="ab-btn ab-btn-primary mt-6 w-full disabled:opacity-50"
            >
              {busy ? 'Please wait' : mode === 'login' ? 'Sign in' : 'Create account'}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
