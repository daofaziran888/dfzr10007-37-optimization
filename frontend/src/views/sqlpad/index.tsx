import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sqlpad'
const columns = [
  "脚本编号",
  "脚本名称",
  "SQL语句",
  "参数定义",
  "执行历史",
  "结果缓存",
  "创建人",
  "脚本状态"
]
const actions = [
  "分享脚本",
  "归档脚本",
  "删除脚本"
]

type Row = Record<string, string | number | null>

type ExecuteResult = {
  ok: boolean
  message: string
  脚本编号: string
  脚本名称: string
  参数: Record<string, string>
  指纹: string
  参数串: string
  来源: string
  缓存结果: string
  执行版本: string
  缓存版本: string
  缓存版本数: number
}

type CacheVersion = {
  指纹: string
  参数: Record<string, string>
  参数串: string
  缓存版本: string
  来源: string
  结果摘要: string
  生成时间: string
}

type HistoryEntry = {
  执行版本: string
  脚本编号: string
  脚本名称: string
  指纹: string
  参数串: string
  来源: string
  缓存结果: string
  缓存版本: string
  时间: string
  说明: string
}

/** 把 “region=华东, period=202609” 这类输入解析成参数对象。 */
function parseParams(text: string): Record<string, string> {
  const params: Record<string, string> = {}
  text.split(/[,，;；&\n]/).forEach((pair) => {
    const [key, ...rest] = pair.split('=')
    const name = (key ?? '').trim()
    const value = rest.join('=').trim()
    if (name && value) {
      params[name] = value
    }
  })
  return params
}

function resultKey(item: Pick<ExecuteResult, '脚本编号' | '参数'>): string {
  return `${item.脚本编号}|${JSON.stringify(item.参数)}`
}

export default function SqlpadView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])
  const [selected, setSelected] = useState<string[]>([])
  const [paramInputs, setParamInputs] = useState<Record<string, string>>({})
  const [batchResults, setBatchResults] = useState<ExecuteResult[] | null>(null)
  const [batchMessage, setBatchMessage] = useState('')
  const [cachePanel, setCachePanel] = useState<{ code: string; items: CacheVersion[] } | null>(null)
  const [historyPanel, setHistoryPanel] = useState<{ code: string; items: HistoryEntry[] } | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('查询脚本列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '自助SQL查询列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const statItems = [
    { label: '查询脚本总数', value: total },
    { label: '已分享脚本', value: rows.filter((row) => row.status === '已分享').length },
    { label: '已归档脚本', value: rows.filter((row) => row.status === '已归档').length },
  ]

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('自助SQL查询动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '自助SQL查询操作失败')
    }
  }

  function toggleSelect(code: string) {
    setSelected((prev) => (prev.includes(code) ? prev.filter((item) => item !== code) : [...prev, code]))
  }

  /** 批量执行：多选脚本整组一次提交，逐条展示缓存结果。 */
  async function runBatch() {
    if (selected.length === 0) {
      setErrorMessage('请先勾选要批量执行的脚本')
      return
    }
    setBusy(true)
    setErrorMessage('')
    const items = selected.map((code) => ({ 脚本编号: code, 参数: parseParams(paramInputs[code] ?? '') }))
    try {
      const response = await request(`${ENDPOINT}/execute/batch`, {
        method: 'POST',
        body: JSON.stringify({ items }),
      })
      if (!response.ok) {
        throw new Error('批量执行请求失败，请稍后重试')
      }
      const payload = await response.json()
      setBatchResults(payload.results ?? [])
      setBatchMessage(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批量执行失败')
    } finally {
      setBusy(false)
    }
  }

  /** 失败脚本只重试自身：用同一份参数单独再执行一次。 */
  async function retryOne(target: ExecuteResult) {
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/execute`, {
        method: 'POST',
        body: JSON.stringify({ 脚本编号: target.脚本编号, 参数: target.参数 }),
      })
      if (!response.ok) {
        throw new Error('重试请求失败，请稍后重试')
      }
      const payload = (await response.json()) as ExecuteResult
      setBatchResults((prev) =>
        prev ? prev.map((item) => (resultKey(item) === resultKey(target) ? payload : item)) : [payload],
      )
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '单脚本重试失败')
    } finally {
      setBusy(false)
    }
  }

  async function showCache(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/cache`)
      if (!response.ok) {
        throw new Error('缓存版本读取失败')
      }
      const payload = await response.json()
      setCachePanel({ code: String(row['脚本编号'] ?? ''), items: payload.items ?? [] })
      setHistoryPanel(null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '缓存版本读取失败')
    }
  }

  async function showHistory(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/history`)
      if (!response.ok) {
        throw new Error('执行历史读取失败')
      }
      const payload = await response.json()
      setHistoryPanel({ code: String(row['脚本编号'] ?? ''), items: payload.items ?? [] })
      setCachePanel(null)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '执行历史读取失败')
    }
  }

  return (
    <section className="page" data-module="sqlpad">
      <header className="page-head">
        <div>
          <h2>自助SQL查询管理</h2>
          <p className="page-desc">结果缓存按脚本编号与参数指纹建立多版本：同一指纹复用唯一快照，参数变化生成新版本，旧参数版本保留可回看。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('查询脚本登记入口尚未接入审批流')}>
            登记查询脚本
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出自助SQL查询清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statItems.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <div className="batch-bar">
        <span>已选 {selected.length} 个脚本，整组一次提交，按脚本与参数指纹归并缓存；失败脚本可单独重试</span>
        <button className="btn primary" type="button" disabled={busy || selected.length === 0} onClick={() => void runBatch()}>
          批量执行选中脚本
        </button>
      </div>

      <table className="data-table">
        <thead>
          <tr>
            <th>选择</th>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>执行参数</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const code = String(row['脚本编号'] ?? '')
            return (
              <tr key={String(row.id)}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected.includes(code)}
                    onChange={() => toggleSelect(code)}
                  />
                </td>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  <input
                    className="param-input"
                    value={paramInputs[code] ?? ''}
                    placeholder="如 region=华东, period=202609"
                    onChange={(event) =>
                      setParamInputs((prev) => ({ ...prev, [code]: event.target.value }))
                    }
                  />
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  <button className="link" type="button" onClick={() => void showCache(row)}>缓存版本</button>
                  <button className="link" type="button" onClick={() => void showHistory(row)}>执行历史</button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无自助SQL查询数据，可先登记查询脚本</td>
            </tr>
          )}
        </tbody>
      </table>

      {batchResults && (
        <section className="panel">
          <header className="panel-head">
            <h3>批量执行结果</h3>
            <button className="link" type="button" onClick={() => setBatchResults(null)}>收起</button>
          </header>
          <p className="muted">{batchMessage}</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>脚本编号</th>
                <th>参数指纹</th>
                <th>缓存结果</th>
                <th>执行版本</th>
                <th>缓存版本</th>
                <th>说明</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {batchResults.map((item) => (
                <tr key={resultKey(item)}>
                  <td>{item.脚本编号}</td>
                  <td>
                    {item.指纹 || '—'}
                    {item.参数串 && <div className="muted">{item.参数串}</div>}
                  </td>
                  <td>
                    <span className={`chip ${item.ok ? (item.缓存结果 === '新建快照' ? 'ok' : 'reuse') : 'fail'}`}>
                      {item.ok ? item.缓存结果 : '失败'}
                    </span>
                  </td>
                  <td>{item.执行版本 || '—'}</td>
                  <td>{item.缓存版本 || '—'}</td>
                  <td>{item.message}</td>
                  <td>
                    {!item.ok && (
                      <button className="link" type="button" disabled={busy} onClick={() => void retryOne(item)}>
                        仅重试本脚本
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {cachePanel && (
        <section className="panel">
          <header className="panel-head">
            <h3>结果缓存版本 · {cachePanel.code}</h3>
            <button className="link" type="button" onClick={() => setCachePanel(null)}>收起</button>
          </header>
          <p className="muted">按脚本编号 + 参数指纹建立多版本缓存，旧参数版本保留可回看。</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>缓存版本</th>
                <th>参数指纹</th>
                <th>参数串</th>
                <th>来源</th>
                <th>结果摘要</th>
                <th>生成时间</th>
              </tr>
            </thead>
            <tbody>
              {cachePanel.items.map((item) => (
                <tr key={item.缓存版本}>
                  <td>{item.缓存版本}</td>
                  <td>{item.指纹}</td>
                  <td>{item.参数串}</td>
                  <td>{item.来源}</td>
                  <td>{item.结果摘要}</td>
                  <td>{item.生成时间}</td>
                </tr>
              ))}
              {cachePanel.items.length === 0 && (
                <tr><td className="empty-state" colSpan={6}>暂无缓存版本，执行一次脚本后生成</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {historyPanel && (
        <section className="panel">
          <header className="panel-head">
            <h3>执行历史 · {historyPanel.code}</h3>
            <button className="link" type="button" onClick={() => setHistoryPanel(null)}>收起</button>
          </header>
          <p className="muted">每个执行版本都标注来源：新建快照、复用快照或同组归并。</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>执行版本</th>
                <th>参数指纹</th>
                <th>来源</th>
                <th>缓存结果</th>
                <th>缓存版本</th>
                <th>时间</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {historyPanel.items.map((item) => (
                <tr key={item.执行版本}>
                  <td>{item.执行版本}</td>
                  <td>
                    {item.指纹}
                    <div className="muted">{item.参数串}</div>
                  </td>
                  <td>{item.来源}</td>
                  <td>{item.缓存结果}</td>
                  <td>{item.缓存版本}</td>
                  <td>{item.时间}</td>
                  <td>{item.说明}</td>
                </tr>
              ))}
              {historyPanel.items.length === 0 && (
                <tr><td className="empty-state" colSpan={7}>暂无执行历史</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条自助SQL查询记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
