import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/retryqueue'
const columns = [
  "条目编号",
  "失败URL",
  "规范化URL",
  "作业编号",
  "作业版本",
  "失败原因码",
  "首次失败时间",
  "已重试次数",
  "下次重试时间",
  "退避级别",
  "最终处置"
]
const actions = [
  "加入队列",
  "发起重试",
  "标记最终失败"
]
const statuses = [
  "草稿",
  "等待重试",
  "重试中",
  "重试成功",
  "最终失败"
]
const scopes = [
  { label: '全部条目', value: '' },
  { label: '仅未收敛', value: 'unconverged' },
  { label: '仅已收敛', value: 'converged' },
]

type Row = Record<string, string | number | boolean | null | undefined>
type ReasonItem = {
  失败原因码: string
  优先级: number
  条目数: number
  未收敛数: number
  条目编号: string[]
}
type ReconcileItem = {
  作业编号: string
  作业版本: string
  进度编号: string
  重试队列URL数: number
  进度URL数: number
  索引清单URL数: number
  未收敛条目数: number
  未收敛URL数: number
  两侧一致: boolean
}

type Filters = {
  keyword: string
  reason: string
  status: string
  job: string
  version: string
  url: string
  scope: string
}

const EMPTY_FILTERS: Filters = {
  keyword: '', reason: '', status: '', job: '', version: '', url: '', scope: '',
}

export default function RetryqueueView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [reasons, setReasons] = useState<ReasonItem[]>([])
  const [reconcile, setReconcile] = useState<ReconcileItem[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)

  const query = useMemo(() => {
    const params = new URLSearchParams()
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value)
    })
    params.set('size', '100')
    return params.toString()
  }, [filters])

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) throw new Error('重试条目列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)

      const reasonQuery = filters.job ? `?job=${encodeURIComponent(filters.job)}` : ''
      const reasonPayload = await (await request(`${ENDPOINT}/reasons${reasonQuery}`)).json()
      setReasons(reasonPayload.items ?? [])

      const reconcilePayload = await (await request(`${ENDPOINT}/reconcile`)).json()
      setReconcile(reconcilePayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试队列列表读取失败')
    }
  }, [query, filters.job])

  useEffect(() => {
    void reload()
  }, [reload])

  const stats = useMemo(() => {
    const waiting = rows.filter((row) => row.status === '等待重试').length
    const converged = rows.filter((row) => row.收敛状态 === '已收敛').length
    const finalFailed = rows.filter((row) => row.status === '最终失败').length
    return [
      { label: '重试条目总数', value: total },
      { label: '等待重试数', value: waiting },
      { label: '已收敛数', value: converged },
      { label: '最终失败数', value: finalFailed },
    ]
  }, [rows, total])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '重试队列动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试队列操作失败')
    }
  }

  function locateByReason(reason: string) {
    setFilters({ ...EMPTY_FILTERS, reason })
  }

  return (
    <section className="page" data-module="retryqueue">
      <header className="page-head">
        <div>
          <h2>重试队列管理</h2>
          <p className="page-desc">
            同一作业版本内一个规范化URL只保留一条重试条目；重试成功或达到退避上限后移出队列，
            未收敛条目才会留在队列里，并同步回写任务执行侧的作业进度。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出重试队列清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        <label className="filter-item">
          <span>条目编号 / URL</span>
          <input
            value={filters.keyword}
            placeholder="按条目编号或URL检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>失败原因码</span>
          <input
            list="reason-codes"
            value={filters.reason}
            placeholder="如 TIMEOUT"
            onChange={(event) => setFilters((prev) => ({ ...prev, reason: event.target.value }))}
          />
          <datalist id="reason-codes">
            {reasons.map((item) => <option key={item.失败原因码} value={item.失败原因码} />)}
          </datalist>
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>作业编号</span>
          <input
            value={filters.job}
            placeholder="如 CRAW-0002"
            onChange={(event) => setFilters((prev) => ({ ...prev, job: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>作业版本</span>
          <input
            value={filters.version}
            placeholder="如 2"
            onChange={(event) => setFilters((prev) => ({ ...prev, version: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>失败URL定位</span>
          <input
            value={filters.url}
            placeholder="输入任意写法的URL"
            onChange={(event) => setFilters((prev) => ({ ...prev, url: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>收敛范围</span>
          <select
            value={filters.scope}
            onChange={(event) => setFilters((prev) => ({ ...prev, scope: event.target.value }))}
          >
            {scopes.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters(EMPTY_FILTERS)}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>原因码</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const codes = (row.失败原因码列表 as string[] | undefined) ?? []
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
                <td className="row-actions">
                  {codes.length === 0 && '—'}
                  {codes.map((code) => (
                    <button key={code} className="link" type="button" onClick={() => locateByReason(code)}>
                      {code}
                    </button>
                  ))}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>没有符合条件且未收敛的重试条目</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">按原因码检索与定位</h3>
      <table className="data-table">
        <thead>
          <tr><th>失败原因码</th><th>优先级</th><th>条目数</th><th>未收敛数</th><th>定位</th></tr>
        </thead>
        <tbody>
          {reasons.map((item) => (
            <tr key={item.失败原因码}>
              <td>{item.失败原因码}</td>
              <td>{item.优先级}</td>
              <td>{item.条目数}</td>
              <td>{item.未收敛数}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => locateByReason(item.失败原因码)}>
                  筛选该原因码
                </button>
                <span className="muted-text">{item.条目编号.join('、')}</span>
              </td>
            </tr>
          ))}
          {reasons.length === 0 && (
            <tr><td className="empty-state" colSpan={5}>暂无原因码统计</td></tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">两侧核对（队列 / 索引清单 / 作业执行进度）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>作业编号</th><th>作业版本</th><th>进度编号</th><th>队列URL数</th>
            <th>进度URL数</th><th>索引清单URL数</th><th>未收敛条目数</th><th>未收敛URL数</th><th>是否一致</th>
          </tr>
        </thead>
        <tbody>
          {reconcile.map((item) => (
            <tr key={`${item.作业编号}-${item.作业版本}`}>
              <td>{item.作业编号}</td>
              <td>{item.作业版本}</td>
              <td>{item.进度编号}</td>
              <td>{item.重试队列URL数}</td>
              <td>{item.进度URL数}</td>
              <td>{item.索引清单URL数}</td>
              <td>{item.未收敛条目数}</td>
              <td>{item.未收敛URL数}</td>
              <td>{item.两侧一致 ? '一致' : '存在差异'}</td>
            </tr>
          ))}
          {reconcile.length === 0 && (
            <tr><td className="empty-state" colSpan={9}>暂无核对数据</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条重试队列记录</span>
        <span>
          {notice && <span className="ok-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
