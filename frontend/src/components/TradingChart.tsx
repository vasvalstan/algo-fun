import { useEffect, useRef, useCallback, useState } from 'react';
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type UTCTimestamp,
  ColorType,
  CrosshairMode,
  type SeriesMarker,
  type ISeriesMarkersPluginApi,
  type Time,
  type AutoscaleInfo,
} from 'lightweight-charts';
import { apiUrl } from '../lib/apiBase';
import type { OhlcCandle, TradeMarker } from '../lib/types';

const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] as const;
type Timeframe = (typeof TIMEFRAMES)[number];

const REFRESH_MS: Record<Timeframe, number> = {
  '1m': 10_000,
  '5m': 30_000,
  '15m': 60_000,
  '1h': 120_000,
  '4h': 300_000,
};

/**
 * Live LIFO grid overlay — renders the "ladder & net" analogy as
 * horizontal price lines on the chart so the user *sees* what the bot
 * is reasoning about (anchor, local high, the resting BUY net, the
 * trail re-arm trigger, every bag's entry + TP). All fields are
 * optional; only the lines whose values are >0 get drawn.
 */
export interface GridOverlay {
  /** Cyan dashed — last rung the climber stood on. */
  readonly anchor?: number;
  /** Purple dotted — highest tick we've seen since last anchor reset. */
  readonly localHigh?: number;
  /**
   * Green solid — the resting BUY currently sitting on the book
   * (the "catching net"). One only.
   */
  readonly restingBuy?: { readonly price: number; readonly tag?: string };
  /**
   * Green dashed — where the next BUY will go once the trail re-arms
   * (or backoff clears). Drawn only when no `restingBuy` is alive.
   */
  readonly pendingBuyTarget?: number;
  /**
   * Amber dashed — the local high needs to climb here before the bot
   * cancels-and-replaces the BUY at a fresh, lower target. Trail
   * trigger from the engine.
   */
  readonly trailTriggerHigh?: number;
  /**
   * Each open bag's entry + matching TP sell. The chart already draws
   * these from `markers` for back-compat, but if the runner sends a
   * `bags` array we use it instead so labels can include bag IDs and
   * TP percentages from the analogy ("🎯 +0.71% sale").
   */
  readonly bags?: ReadonlyArray<{
    readonly bagId: number;
    readonly entry: number;
    readonly tp: number;
  }>;
  /** TP percent — purely cosmetic, used in the bag TP label. */
  readonly tpPct?: number;
  /** Dip percent — used in the BUY (net) label so it reads "🥅 BUY −0.75%". */
  readonly dipPct?: number;
}

interface Props {
  readonly markers?: TradeMarker[];
  readonly height?: number;
  readonly candlesEndpoint?: string;
  /**
   * When provided, the chart draws the LIFO "ladder & net" overlay
   * (anchor, local high, resting BUY, pending BUY target, trail
   * trigger, plus every bag's entry + TP) and SKIPS the legacy
   * marker-derived price lines so they don't double-stack with the
   * overlay's bag lines. Pass null/undefined on charts that don't run
   * the LIFO grid (e.g. the v2 multi-strategy paper sandbox).
   */
  readonly gridOverlay?: GridOverlay | null;
}

function toUtc(t: number): UTCTimestamp {
  return t as UTCTimestamp;
}

/**
 * Interval in seconds for each timeframe — used to snap trade markers
 * to the correct candle boundary.
 */
const TF_SECONDS: Record<Timeframe, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
};

/**
 * Tiny inline key for the LIFO grid overlay so the emojis on the
 * price-axis labels (🪜 🏔️ 🥅 ⏫ 🪤 💰) read as "ladder, high water,
 * net, re-arm trigger, caught bag, sell ticket". Only the items that
 * are actually being drawn show up in the legend, so the bar stays
 * uncluttered when the bot is fully flat.
 */
function GridOverlayLegend({ overlay }: { readonly overlay: GridOverlay }) {
  const items: Array<{ glyph: string; label: string; color: string }> = [];
  if (overlay.anchor && overlay.anchor > 0) {
    items.push({ glyph: '🪜', label: 'Anchor', color: 'rgba(34, 211, 238, 0.85)' });
  }
  if (
    overlay.localHigh
    && overlay.localHigh > 0
    && overlay.localHigh !== overlay.anchor
  ) {
    items.push({ glyph: '🏔️', label: 'High water', color: 'rgba(167, 139, 250, 0.85)' });
  }
  if (overlay.restingBuy && overlay.restingBuy.price > 0) {
    items.push({ glyph: '🥅', label: 'BUY net (live)', color: 'rgba(34, 197, 94, 0.95)' });
  } else if (overlay.pendingBuyTarget && overlay.pendingBuyTarget > 0) {
    items.push({ glyph: '🥅', label: 'BUY target', color: 'rgba(34, 197, 94, 0.7)' });
  }
  if (overlay.trailTriggerHigh && overlay.trailTriggerHigh > 0) {
    items.push({ glyph: '⏫', label: 'Re-arm trigger', color: 'rgba(245, 158, 11, 0.85)' });
  }
  const bagCount = overlay.bags?.filter((b) => b.entry > 0).length ?? 0;
  if (bagCount > 0) {
    items.push({ glyph: '🪤', label: `Caught (${bagCount})`, color: 'rgba(34, 197, 94, 0.7)' });
    items.push({ glyph: '💰', label: 'TP sale', color: 'rgba(234, 179, 8, 0.85)' });
  }
  if (items.length === 0) return null;
  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 10,
        marginLeft: 'auto',
        fontSize: '0.62rem',
        fontFamily: "'JetBrains Mono', monospace",
        color: 'var(--text-muted)',
        alignItems: 'center',
      }}
    >
      {items.map((it) => (
        <span key={it.label} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
          <span aria-hidden style={{ fontSize: '0.85rem', lineHeight: 1 }}>{it.glyph}</span>
          <span style={{ color: it.color }}>{it.label}</span>
        </span>
      ))}
    </div>
  );
}

interface ZoneBox {
  readonly id: string;
  readonly top: number;
  readonly height: number;
  readonly fill: string;
  readonly border: string;
  readonly label: string;
  readonly labelColor: string;
  readonly live: boolean;
}

export function TradingChart({ markers, height = 400, candlesEndpoint = '/api/paper-v2/candles', gridOverlay }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const priceLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([]);

  const [tf, setTf] = useState<Timeframe>('5m');
  const [candles, setCandles] = useState<OhlcCandle[]>([]);
  const [loading, setLoading] = useState(false);
  /**
   * Filled price-zone rectangles overlaid on the chart canvas
   * (TradingView "Open P&L" style). One green box per open bag
   * spanning entry → TP, plus a faint green box for the live BUY
   * net spanning resting price → projected TP. Positions are pixel
   * coordinates re-derived from `priceToCoordinate` at ~4Hz so they
   * track autoscale smoothly.
   */
  const [zones, setZones] = useState<ZoneBox[]>([]);
  /** Right-axis pixel width — needed so zone boxes stop short of the
   * price labels instead of running underneath them. */
  const [rightAxisWidth, setRightAxisWidth] = useState(0);

  // Fetch candles from REST API
  const fetchCandles = useCallback(async (interval: Timeframe, showLoader = false) => {
    if (showLoader) setLoading(true);
    try {
      const res = await fetch(apiUrl(`${candlesEndpoint}?interval=${interval}`));
      if (!res.ok) return;
      const data = await res.json();
      if (data.ok && Array.isArray(data.candles)) {
        setCandles(data.candles as OhlcCandle[]);
      }
    } catch {
      /* network error — will retry on next interval */
    } finally {
      if (showLoader) setLoading(false);
    }
  }, [candlesEndpoint]);

  // Fetch on mount + when timeframe changes
  useEffect(() => {
    void fetchCandles(tf, true);
    const timer = setInterval(() => void fetchCandles(tf), REFRESH_MS[tf]);
    return () => clearInterval(timer);
  }, [tf, fetchCandles]);

  // Create chart once
  const initChart = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const chart = createChart(container, {
      width: container.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: 'rgba(255, 255, 255, 0.5)',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(255, 255, 255, 0.04)' },
        horzLines: { color: 'rgba(255, 255, 255, 0.04)' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: 'rgba(255, 255, 255, 0.15)',
          labelBackgroundColor: 'rgba(30, 30, 40, 0.9)',
        },
        horzLine: {
          color: 'rgba(255, 255, 255, 0.15)',
          labelBackgroundColor: 'rgba(30, 30, 40, 0.9)',
        },
      },
      rightPriceScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        scaleMargins: { top: 0.08, bottom: 0.22 },
      },
      timeScale: {
        borderColor: 'rgba(255, 255, 255, 0.08)',
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 3,
        barSpacing: 7,
        minBarSpacing: 3,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#16a34a',
      borderDownColor: '#dc2626',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });

    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    const markersPlugin = createSeriesMarkers(candleSeries, []);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;
    markersPluginRef.current = markersPlugin;

    resizeObserverRef.current = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        if (w > 0) chart.applyOptions({ width: w });
      }
    });
    resizeObserverRef.current.observe(container);
  }, [height]);

  useEffect(() => {
    initChart();
    return () => {
      resizeObserverRef.current?.disconnect();
      chartRef.current?.remove();
      chartRef.current = null;
    };
  }, [initChart]);

  // Update candle + volume data
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    const chart = chartRef.current;
    if (!candleSeries || !volumeSeries || !chart || candles.length === 0) return;

    const candleData: CandlestickData<UTCTimestamp>[] = candles.map((c) => ({
      time: toUtc(c.time),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    const volumeData: HistogramData<UTCTimestamp>[] = candles.map((c) => ({
      time: toUtc(c.time),
      value: c.volume,
      color: c.close >= c.open ? 'rgba(34, 197, 94, 0.3)' : 'rgba(239, 68, 68, 0.3)',
    }));

    candleSeries.setData(candleData);
    volumeSeries.setData(volumeData);

    // Auto-fit the visible range to the last ~60 bars
    const visibleBars = Math.min(60, candles.length);
    const fromIdx = candles.length - visibleBars;
    chart.timeScale().setVisibleRange({
      from: toUtc(candles[fromIdx].time),
      to: toUtc(candles[candles.length - 1].time),
    });
  }, [candles]);

  // Trade markers
  useEffect(() => {
    const plugin = markersPluginRef.current;
    if (!plugin) return;

    if (!markers || markers.length === 0 || candles.length === 0) {
      plugin.setMarkers([]);
      return;
    }

    const snap = TF_SECONDS[tf];
    const candleTimeSet = new Set(candles.map((c) => c.time));

    const chartMarkers: SeriesMarker<Time>[] = markers
      .filter((m) => {
        const snapped = Math.floor(m.time / snap) * snap;
        return candleTimeSet.has(snapped);
      })
      .map((m) => ({
        time: toUtc(Math.floor(m.time / snap) * snap),
        position: m.position,
        color: m.color,
        shape: m.shape,
        text: m.text,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    plugin.setMarkers(chartMarkers);
  }, [candles, markers, tf]);

  // Price lines for active positions / live LIFO grid overlay
  // ────────────────────────────────────────────────────────────
  // Two modes:
  //   1. `gridOverlay` is provided → draw the full "ladder & net"
  //      analogy: anchor, local high, resting BUY (the net), pending
  //      BUY target, trail re-arm trigger, plus every bag's entry +
  //      matching TP. This makes the chart self-explanatory: the user
  //      can SEE where the net is hanging and where each catch is
  //      pre-listed for sale.
  //   2. No overlay (legacy / paper-v2 charts) → fall back to the
  //      original behaviour of one entry + TP line per active buy
  //      marker.
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    if (!candleSeries) return;

    for (const pl of priceLinesRef.current) {
      try { candleSeries.removePriceLine(pl); } catch { /* */ }
    }
    priceLinesRef.current = [];

    // Collect every overlay price level so we can (1) draw price
    // lines at them and (2) force the candle series' autoscale to
    // include them — otherwise levels above/below the visible candle
    // range only appear as edge labels with no horizontal line drawn
    // through the chart body, which is exactly the bug we just hit.
    const overlayLevels: number[] = [];

    if (gridOverlay) {
      const push = (
        opts: Parameters<ISeriesApi<'Candlestick'>['createPriceLine']>[0],
      ) => {
        priceLinesRef.current.push(candleSeries.createPriceLine(opts));
        if (typeof opts.price === 'number' && opts.price > 0) {
          overlayLevels.push(opts.price);
        }
      };

      if (gridOverlay.anchor && gridOverlay.anchor > 0) {
        push({
          price: gridOverlay.anchor,
          color: 'rgba(34, 211, 238, 0.95)',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: '🪜 Anchor',
        });
      }

      if (
        gridOverlay.localHigh
        && gridOverlay.localHigh > 0
        && gridOverlay.localHigh !== gridOverlay.anchor
      ) {
        push({
          price: gridOverlay.localHigh,
          color: 'rgba(167, 139, 250, 0.85)',
          lineWidth: 1,
          lineStyle: 3,
          axisLabelVisible: true,
          title: '🏔️ High',
        });
      }

      if (gridOverlay.restingBuy && gridOverlay.restingBuy.price > 0) {
        const dipLabel = typeof gridOverlay.dipPct === 'number'
          ? ` −${gridOverlay.dipPct.toFixed(2)}%`
          : '';
        push({
          price: gridOverlay.restingBuy.price,
          color: 'rgba(34, 197, 94, 1)',
          lineWidth: 3,
          lineStyle: 0,
          axisLabelVisible: true,
          title: `🥅 BUY (net)${dipLabel}`,
        });
      } else if (gridOverlay.pendingBuyTarget && gridOverlay.pendingBuyTarget > 0) {
        push({
          price: gridOverlay.pendingBuyTarget,
          color: 'rgba(34, 197, 94, 0.9)',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: '🥅 BUY target',
        });
      }

      if (gridOverlay.trailTriggerHigh && gridOverlay.trailTriggerHigh > 0) {
        push({
          price: gridOverlay.trailTriggerHigh,
          color: 'rgba(245, 158, 11, 0.95)',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: '⏫ re-arm',
        });
      }

      const tpLabel = typeof gridOverlay.tpPct === 'number'
        ? ` +${gridOverlay.tpPct.toFixed(2)}%`
        : '';
      const bags = gridOverlay.bags ?? [];
      for (const bag of bags) {
        if (bag.entry > 0) {
          push({
            price: bag.entry,
            color: 'rgba(34, 197, 94, 0.85)',
            lineWidth: 2,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `🪤 #${bag.bagId} caught`,
          });
        }
        if (bag.tp > 0) {
          push({
            price: bag.tp,
            color: 'rgba(234, 179, 8, 0.95)',
            lineWidth: 2,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `💰 #${bag.bagId} sale${tpLabel}`,
          });
        }
      }
    } else {
      const activeBuys = markers?.filter((m) => m.side === 'buy' && m.active) ?? [];
      for (const buy of activeBuys) {
        priceLinesRef.current.push(
          candleSeries.createPriceLine({
            price: buy.price,
            color: 'rgba(34, 197, 94, 0.6)',
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: `Entry #${buy.text.replace('Buy #', '')}`,
          }),
        );

        if (buy.tp_price) {
          priceLinesRef.current.push(
            candleSeries.createPriceLine({
              price: buy.tp_price,
              color: 'rgba(234, 179, 8, 0.6)',
              lineWidth: 1,
              lineStyle: 2,
              axisLabelVisible: true,
              title: `TP #${buy.text.replace('Buy #', '')}`,
            }),
          );
        }
      }
    }

    // Re-bind autoscale so the candle series pulls Anchor / BUY-net /
    // re-arm trigger / etc. into the visible Y range. Without this the
    // axis labels for out-of-range levels render at the chart edge but
    // their horizontal lines never get painted (you can see this
    // happen in dev tools as `top` going negative or > height).
    const levels = overlayLevels.filter((v) => Number.isFinite(v) && v > 0);
    candleSeries.applyOptions({
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const base = original();
        if (levels.length === 0) return base;
        const baseMin = base?.priceRange?.minValue;
        const baseMax = base?.priceRange?.maxValue;
        const min = typeof baseMin === 'number'
          ? Math.min(baseMin, ...levels)
          : Math.min(...levels);
        const max = typeof baseMax === 'number'
          ? Math.max(baseMax, ...levels)
          : Math.max(...levels);
        // Add a tiny visual padding (~0.5% of span) so the topmost /
        // bottommost overlay line doesn't kiss the chart frame.
        const pad = (max - min) * 0.005;
        return {
          ...(base ?? {}),
          priceRange: { minValue: min - pad, maxValue: max + pad },
        };
      },
    });
  }, [markers, gridOverlay]);

  // ── Zone-box overlay (TradingView "Open P&L" style filled rects) ──
  // Lightweight-charts has no built-in rectangle primitive, so we
  // overlay absolute-positioned <div>s and translate prices → pixels
  // via `priceToCoordinate`. Recomputed at ~4Hz so it tracks any
  // autoscale shift caused by ticks pushing the price range without
  // requiring a full React re-render of the chart itself.
  useEffect(() => {
    const recompute = () => {
      const series = candleSeriesRef.current;
      const chart = chartRef.current;
      if (!series || !chart) {
        setZones([]);
        setRightAxisWidth(0);
        return;
      }
      try {
        setRightAxisWidth(chart.priceScale('right').width());
      } catch {
        /* chart torn down between RAFs */
      }
      if (!gridOverlay) {
        setZones([]);
        return;
      }

      const next: ZoneBox[] = [];
      const tpPct = gridOverlay.tpPct;

      for (const bag of gridOverlay.bags ?? []) {
        if (bag.entry <= 0 || bag.tp <= 0) continue;
        const yTp = series.priceToCoordinate(bag.tp);
        const yEntry = series.priceToCoordinate(bag.entry);
        if (yTp == null || yEntry == null) continue;
        const top = Math.min(yTp, yEntry);
        const bottom = Math.max(yTp, yEntry);
        if (bottom - top < 1) continue;
        next.push({
          id: `bag-${bag.bagId}`,
          top,
          height: bottom - top,
          fill: 'rgba(34, 197, 94, 0.14)',
          border: 'rgba(34, 197, 94, 0.55)',
          labelColor: 'rgba(134, 239, 172, 0.95)',
          label: typeof tpPct === 'number'
            ? `🪤 #${bag.bagId} → 💰 +${tpPct.toFixed(2)}%`
            : `🪤 #${bag.bagId} profit zone`,
          live: true,
        });
      }

      // Projected catch zone for the live resting BUY (the "net").
      // Drawn faintly so the active bag boxes still pop visually.
      if (
        gridOverlay.restingBuy
        && gridOverlay.restingBuy.price > 0
        && typeof tpPct === 'number'
      ) {
        const buy = gridOverlay.restingBuy.price;
        const projectedTp = buy * (1 + tpPct / 100);
        const yTp = series.priceToCoordinate(projectedTp);
        const yBuy = series.priceToCoordinate(buy);
        if (yTp != null && yBuy != null) {
          const top = Math.min(yTp, yBuy);
          const bottom = Math.max(yTp, yBuy);
          if (bottom - top >= 1) {
            next.push({
              id: 'pending-net',
              top,
              height: bottom - top,
              fill: 'rgba(34, 197, 94, 0.06)',
              border: 'rgba(34, 197, 94, 0.30)',
              labelColor: 'rgba(134, 239, 172, 0.7)',
              label: `🥅 catch zone → +${tpPct.toFixed(2)}%`,
              live: false,
            });
          }
        }
      }

      setZones(next);
    };

    recompute();
    const chart = chartRef.current;
    const onRange = () => recompute();
    chart?.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    // 4Hz refresh — autoscale isn't subscribable, and pollling at
    // this rate is cheap (a few setState calls on small arrays).
    const interval = window.setInterval(recompute, 250);
    return () => {
      chart?.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      window.clearInterval(interval);
    };
  }, [gridOverlay, candles]);

  return (
    <div>
      {/* Timeframe bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: '8px 0',
      }}>
        {TIMEFRAMES.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTf(t)}
            style={{
              padding: '4px 12px',
              borderRadius: 4,
              border: tf === t ? '1px solid var(--purple-400)' : '1px solid rgba(255,255,255,0.08)',
              background: tf === t ? 'rgba(167,139,250,0.15)' : 'transparent',
              color: tf === t ? 'var(--purple-300)' : 'var(--text-muted)',
              fontWeight: tf === t ? 600 : 400,
              fontSize: '0.72rem',
              fontFamily: "'JetBrains Mono', monospace",
              cursor: 'pointer',
              transition: 'all 0.15s',
            }}
          >
            {t}
          </button>
        ))}
        {loading && (
          <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)', marginLeft: 8 }}>Loading…</span>
        )}
        {gridOverlay && <GridOverlayLegend overlay={gridOverlay} />}
      </div>

      {/* Chart container + zone-box overlay layer */}
      <div
        style={{
          position: 'relative',
          width: '100%',
          height,
          borderRadius: 'var(--radius-sm)',
          overflow: 'hidden',
        }}
      >
        <div
          ref={containerRef}
          style={{ width: '100%', height: '100%' }}
        />
        {zones.length > 0 && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              pointerEvents: 'none',
            }}
          >
            {zones.map((z) => (
              <div
                key={z.id}
                style={{
                  position: 'absolute',
                  top: z.top,
                  height: z.height,
                  left: 0,
                  right: rightAxisWidth,
                  background: z.fill,
                  borderTop: `1px dashed ${z.border}`,
                  borderBottom: `1px dashed ${z.border}`,
                  boxSizing: 'border-box',
                }}
              >
                <span
                  style={{
                    position: 'absolute',
                    top: 3,
                    right: 8,
                    padding: '2px 6px',
                    borderRadius: 3,
                    background: 'rgba(0, 0, 0, 0.55)',
                    color: z.labelColor,
                    fontSize: '0.62rem',
                    fontFamily: "'JetBrains Mono', monospace",
                    fontWeight: z.live ? 700 : 500,
                    letterSpacing: '0.04em',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {z.label}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
