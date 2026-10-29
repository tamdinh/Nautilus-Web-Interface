import React, { useState, useEffect } from 'react';
import api from '../lib/api';

interface Strategy {
  id: string;
  name: string;
  type: string;
  status: string;
  description: string;
  config: Record<string, any>;
  performance: {
    total_pnl: number;
    total_trades: number;
    win_rate: number;
  };
}

interface StrategyType {
  id: string;
  label: string;
  description: string;
  default_config: Record<string, string | number>;
}

const FALLBACK_TYPES: StrategyType[] = [
  { id: 'sma_crossover', label: 'SMA Crossover', description: '', default_config: { instrument_id: 'EUR/USD.SIM', bar_type: 'EUR/USD.SIM-1-MINUTE-BID-INTERNAL', fast_period: 10, slow_period: 20, trade_size: '100000' } },
  { id: 'rsi', label: 'RSI Mean-Reversion', description: '', default_config: { instrument_id: 'EUR/USD.SIM', bar_type: 'EUR/USD.SIM-1-MINUTE-BID-INTERNAL', rsi_period: 14, oversold_level: 30, overbought_level: 70, trade_size: '100000' } },
  { id: 'macd', label: 'MACD Crossover', description: '', default_config: { instrument_id: 'EUR/USD.SIM', bar_type: 'EUR/USD.SIM-1-MINUTE-BID-INTERNAL', fast_period: 12, slow_period: 26, signal_period: 9, trade_size: '100000' } },
];

export default function StrategiesPage() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [modalError, setModalError] = useState<string | null>(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [types, setTypes] = useState<StrategyType[]>(FALLBACK_TYPES);
  const [newStrategy, setNewStrategy] = useState({
    name: '',
    type: 'sma_crossover',
    description: '',
  });
  const [params, setParams] = useState<Record<string, string | number>>(FALLBACK_TYPES[0].default_config);

  useEffect(() => {
    fetchStrategies();
    api
      .get<{ types: StrategyType[] }>('/api/strategy-types')
      .then((r) => {
        if (r.types?.length) {
          setTypes(r.types);
          const first = r.types.find((t) => t.id === 'sma_crossover') ?? r.types[0];
          setNewStrategy((s) => ({ ...s, type: first.id }));
          setParams(first.default_config);
        }
      })
      .catch(() => undefined); // fall back to the built-in list
  }, []);

  const selectType = (id: string) => {
    setNewStrategy({ ...newStrategy, type: id });
    setParams(types.find((t) => t.id === id)?.default_config ?? {});
  };

  const fetchStrategies = async () => {
    try {
      const data = await api.get<{ strategies: Strategy[] }>('/api/strategies');
      setStrategies(data.strategies || []);
      setFetchError(null);
    } catch (error) {
      setFetchError(error instanceof Error ? error.message : 'Failed to load strategies');
    } finally {
      setLoading(false);
    }
  };

  const handleAddStrategy = async () => {
    setModalError(null);
    try {
      // Backend reads the parameters as top-level fields; numeric ones must be numbers
      const defaults = types.find((t) => t.id === newStrategy.type)?.default_config ?? {};
      const body: Record<string, unknown> = { ...newStrategy };
      for (const [key, value] of Object.entries(params)) {
        body[key] = typeof defaults[key] === 'number' ? Number(value) : String(value);
      }
      await api.post('/api/strategies', body);
      setShowAddModal(false);
      setNewStrategy({ ...newStrategy, name: '', description: '' });
      setParams(defaults);
      fetchStrategies();
    } catch (error) {
      setModalError(error instanceof Error ? error.message : 'Failed to add strategy');
    }
  };

  const handleStartStrategy = async (strategyId: string) => {
    try {
      await api.post(`/api/strategies/${strategyId}/start`);
      fetchStrategies();
    } catch (error) {
      setFetchError(error instanceof Error ? error.message : 'Failed to start strategy');
    }
  };

  const handleStopStrategy = async (strategyId: string) => {
    try {
      await api.post(`/api/strategies/${strategyId}/stop`);
      fetchStrategies();
    } catch (error) {
      setFetchError(error instanceof Error ? error.message : 'Failed to stop strategy');
    }
  };

  const handleDeleteStrategy = async (strategyId: string) => {
    if (!confirm('Are you sure you want to delete this strategy?')) return;
    try {
      await api.delete(`/api/strategies/${strategyId}`);
      fetchStrategies();
    } catch (error) {
      setFetchError(error instanceof Error ? error.message : 'Failed to delete strategy');
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50 p-8">
        <div className="text-center">Loading strategies...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50 p-8">
      <div className="max-w-7xl mx-auto">
        {fetchError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 rounded-lg px-4 py-3 text-sm">
            {fetchError}
          </div>
        )}
        <div className="mb-4 bg-blue-50 border border-blue-200 text-blue-900 rounded-lg px-4 py-3 text-sm">
          Strategies are tested in <b>Backtesting</b> on simulated data. <b>Start</b> only marks a strategy as active
          (used by the daily-loss halt); it does <b>not</b> place paper or live orders yet. Live orders are placed
          manually from the Orders page.
        </div>
        {/* Header */}
        <div className="flex justify-between items-center mb-8">
          <div>
            <h1 className="text-4xl font-bold text-gray-900 mb-2">📈 Strategy Management</h1>
            <p className="text-gray-600">Manage and monitor your trading strategies</p>
          </div>
          <div className="flex gap-4">
            <button
              onClick={() => window.location.href = '/trader'}
              className="px-6 py-3 bg-white border-2 border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-all font-semibold"
            >
              ← Back to Dashboard
            </button>
            <button
              onClick={() => setShowAddModal(true)}
              className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-all font-semibold"
            >
              + Add Strategy
            </button>
          </div>
        </div>

        {/* Strategies Grid */}
        {strategies.length === 0 ? (
          <div className="bg-white rounded-xl shadow-lg p-12 text-center">
            <div className="text-6xl mb-4">📊</div>
            <h3 className="text-2xl font-bold text-gray-900 mb-2">No Strategies Yet</h3>
            <p className="text-gray-600 mb-6">Add your first trading strategy to get started</p>
            <button
              onClick={() => setShowAddModal(true)}
              className="px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-all font-semibold"
            >
              + Add Strategy
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {strategies.map((strategy) => (
              <div key={strategy.id} className="bg-white rounded-xl shadow-lg p-6 hover:shadow-xl transition-shadow">
                <div className="flex justify-between items-start mb-4">
                  <div>
                    <h3 className="text-xl font-bold text-gray-900">{strategy.name}</h3>
                    <p className="text-sm text-gray-500">{strategy.type}</p>
                  </div>
                  <span className={`px-3 py-1 rounded-full text-sm font-semibold ${
                    strategy.status === 'running' 
                      ? 'bg-green-100 text-green-800' 
                      : 'bg-gray-100 text-gray-800'
                  }`}>
                    {strategy.status === 'running' ? 'active (no orders)' : strategy.status}
                  </span>
                </div>

                <p className="text-gray-600 text-sm mb-2">{strategy.description || 'No description'}</p>
                {strategy.config && Object.keys(strategy.config).length > 0 && (
                  <p className="text-xs text-gray-500 mb-4 font-mono break-words">
                    {Object.entries(strategy.config)
                      .filter(([k]) => k !== 'bar_type')
                      .map(([k, v]) => `${k}=${v}`)
                      .join(' · ')}
                  </p>
                )}

                {/* Performance Metrics */}
                <div className="bg-gray-50 rounded-lg p-4 mb-4">
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div>
                      <div className="text-xs text-gray-500">P&L</div>
                      <div className={`font-bold ${(strategy.performance?.total_pnl ?? 0) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                        ${(strategy.performance?.total_pnl ?? 0).toFixed(2)}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs text-gray-500">Trades</div>
                      <div className="font-bold text-gray-900">{strategy.performance?.total_trades ?? 0}</div>
                    </div>
                    <div>
                      <div className="text-xs text-gray-500">Win Rate</div>
                      <div className="font-bold text-blue-600">{((strategy.performance?.win_rate ?? 0) * 100).toFixed(1)}%</div>
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex gap-2">
                  {strategy.status === 'stopped' ? (
                    <button
                      onClick={() => handleStartStrategy(strategy.id)}
                      className="flex-1 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition-all font-semibold text-sm"
                    >
                      ▶ Start
                    </button>
                  ) : (
                    <button
                      onClick={() => handleStopStrategy(strategy.id)}
                      className="flex-1 px-4 py-2 bg-yellow-600 text-white rounded-lg hover:bg-yellow-700 transition-all font-semibold text-sm"
                    >
                      ⏸ Stop
                    </button>
                  )}
                  <button
                    onClick={() => handleDeleteStrategy(strategy.id)}
                    className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-all font-semibold text-sm"
                  >
                    🗑️
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Add Strategy Modal */}
        {showAddModal && (
          <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
            <div className="bg-white rounded-xl shadow-2xl p-8 max-w-md w-full max-h-[90vh] overflow-y-auto">
              <h2 className="text-2xl font-bold text-gray-900 mb-6">Add New Strategy</h2>
              
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-2">Strategy Name</label>
                  <input
                    type="text"
                    value={newStrategy.name}
                    onChange={(e) => setNewStrategy({ ...newStrategy, name: e.target.value })}
                    className="w-full px-4 py-2 border-2 border-gray-300 rounded-lg focus:border-blue-500 focus:outline-none"
                    placeholder="My Trading Strategy"
                  />
                </div>

                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-2">Strategy Type</label>
                  <select
                    value={newStrategy.type}
                    onChange={(e) => selectType(e.target.value)}
                    className="w-full px-4 py-2 border-2 border-gray-300 rounded-lg focus:border-blue-500 focus:outline-none"
                  >
                    {types.map((t) => (
                      <option key={t.id} value={t.id}>{t.label}</option>
                    ))}
                  </select>
                  {types.find((t) => t.id === newStrategy.type)?.description && (
                    <p className="mt-1 text-xs text-gray-500">{types.find((t) => t.id === newStrategy.type)?.description}</p>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(params).map(([key, value]) => {
                    const numeric = typeof (types.find((t) => t.id === newStrategy.type)?.default_config ?? {})[key] === 'number';
                    return (
                      <div key={key} className={key === 'bar_type' ? 'col-span-2' : ''}>
                        <label className="block text-xs font-semibold text-gray-700 mb-1">{key.replace(/_/g, ' ')}</label>
                        <input
                          type={numeric ? 'number' : 'text'}
                          step="any"
                          value={value}
                          onChange={(e) => setParams({ ...params, [key]: e.target.value })}
                          className="w-full px-3 py-2 border-2 border-gray-300 rounded-lg focus:border-blue-500 focus:outline-none text-sm"
                        />
                      </div>
                    );
                  })}
                </div>

                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-2">Description</label>
                  <textarea
                    value={newStrategy.description}
                    onChange={(e) => setNewStrategy({ ...newStrategy, description: e.target.value })}
                    className="w-full px-4 py-2 border-2 border-gray-300 rounded-lg focus:border-blue-500 focus:outline-none"
                    rows={3}
                    placeholder="Describe your strategy..."
                  />
                </div>
              </div>

              {modalError && (
                <div className="mt-4 bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2 text-sm">
                  {modalError}
                </div>
              )}
              <div className="flex gap-4 mt-6">
                <button
                  onClick={() => { setShowAddModal(false); setModalError(null); }}
                  className="flex-1 px-6 py-3 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 transition-all font-semibold"
                >
                  Cancel
                </button>
                <button
                  onClick={handleAddStrategy}
                  className="flex-1 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-all font-semibold"
                  disabled={!newStrategy.name}
                >
                  Add Strategy
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

