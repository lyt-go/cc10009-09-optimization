import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/taskexec'
const progressQueryBase = '/api/taskexec/progress'
const manifestQueryBase = '/api/taskexec/manifest'
const resultsQueryBase = '/api/taskexec/results'

type Row = Record<string, unknown>

type AuditDetail = {
  规范化URL: string
  失败URL: string
  失败原因码: string
  结果状态: string
  索引编号: string
  重试结果编号: string
  重试结果: string
  进度编号: string
  作业编号: string
  作业版本: string
  迟到回写: boolean
}

type Audit = {
  作业编号: string
  作业版本: string
  进度编号: string
  进度状态: string
  唯一URL集合: string[]
  计数: Record<string, number>
  明细: AuditDetail[]
  版本依据: { 作业版本: string; 来源: string; 规范化URL: string; 失败原因码: string }[]
  冲突标记: boolean
  冲突处置: string
  核对: Record<string, boolean | number>
}

const stated: Record<string, string> = {
  执行中: '执行中',
  已完成: '已完成',
  已停止: '已停止',
}

function cell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function TaskexecView() {
  const [progressRows, setProgressRows] = useState<Row[]>([])
  const [manifestRows, setManifestRows] = useState<Row[]>([])
  const [resultRows, setResultRows] = useState<Row[]>([])
  const [audit, setAudit] = useState<Audit | null>(null)
  const [selectedKey, setSelectedKey] = useState('')
  const [job, setJob] = useState('')
  const [version, setVersion] = useState('')
  const [state, setState] = useState('')
  const [keyword, setKeyword] = useState('')
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')

  const common = useMemo(() => {
    const params = new URLSearchParams()
    if (job) params.set('job', job)
    if (version) params.set('version', version)
    return params
  }, [job, version])

  // 只保存「作业编号|版本」这样的字符串键，避免每次刷新都因为新对象引用而重跑。
  const auditKey = useMemo(() => {
    if (!selectedKey) return ''
    const [jobNo, jobVersion] = selectedKey.split('|')
    return `${ENDPOINT}/audit?job=${encodeURIComponent(jobNo)}&version=${encodeURIComponent(jobVersion ?? '1')}`
  }, [selectedKey])

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const progressPayload = await (
        await request(`${progressQueryBase}?${common.toString()}&size=100`)
      ).json()
      setProgressRows(progressPayload.items ?? [])

      const manifestParams = new URLSearchParams(common)
      if (state) manifestParams.set('state', state)
      if (keyword) manifestParams.set('keyword', keyword)
      manifestParams.set('size', '100')
      const manifestPayload = await (await request(`${manifestQueryBase}?${manifestParams.toString()}`)).json()
      setManifestRows(manifestPayload.items ?? [])

      const resultPayload = await (
        await request(`${resultsQueryBase}?${common.toString()}&size=100`)
      ).json()
      setResultRows(resultPayload.items ?? [])

      if (!selectedKey) {
        const first = progressPayload.items?.[0]
        if (first) setSelectedKey(`${first.作业编号}|${first.作业版本}`)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '任务执行侧数据读取失败')
    }
  }, [common, state, keyword, selectedKey])

  const loadAudit = useCallback(async () => {
    if (!auditKey) {
      setAudit(null)
      return
    }
    try {
      const payload = await (await request(auditKey)).json()
      setAudit(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '核对明细读取失败')
    }
  }, [auditKey])

  useEffect(() => {
    void reload()
    void loadAudit()
  }, [reload, loadAudit])

  async function openAudit(row: Row) {
    setSelectedKey(`${row.作业编号}|${row.作业版本}`)
    setNotice('')
  }

  async function reopen(row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${resultsQueryBase}`, {
        method: 'POST',
        body: JSON.stringify({
          作业编号: row.作业编号,
          作业版本: row.作业版本,
          失败URL: row.失败URL,
          结果: '成功',
        }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '重试结果回写失败')
      }
      setNotice(payload.message ?? '')
      await reload()
      await loadAudit()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试结果回写失败')
    }
  }

  const stats = useMemo(() => {
    const totalUrls = progressRows.reduce((sum, row) => sum + Number(row.总URL数 ?? 0), 0)
    const unfinished = progressRows.reduce((sum, row) => sum + Number(row.未收敛URL数 ?? 0), 0)
    const conflicted = progressRows.filter((row) => row.冲突标记).length
    return [
      { label: '作业进度条数', value: progressRows.length },
      { label: '唯一URL总数', value: totalUrls },
      { label: '未收敛URL数', value: unfinished },
      { label: '版本冲突进度', value: conflicted },
    ]
  }, [progressRows])

  return (
    <section className="page" data-module="taskexec">
      <header className="page-head">
        <div>
          <h2>任务执行侧</h2>
          <p className="page-desc">
            读取重试结果并回写原作业进度：作业执行进度按「作业编号 + 作业版本」唯一，
            结果索引清单落到任务执行侧，可从清单回查进度；作业结束后晚到的结果只归档。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn"
            type="button"
            onClick={() => window.open(`${ENDPOINT}/dataset/progress${job ? `?job=${encodeURIComponent(job)}` : ''}`, '_blank')}
          >
            导出作业执行进度数据集
          </button>
          <button
            className="btn"
            type="button"
            onClick={() => window.open(`${ENDPOINT}/dataset/retry-results${job ? `?job=${encodeURIComponent(job)}` : ''}`, '_blank')}
          >
            导出重试结果数据集
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
          <span>作业编号</span>
          <input value={job} placeholder="如 CRAW-0002" onChange={(event) => setJob(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>作业版本</span>
          <input value={version} placeholder="如 2" onChange={(event) => setVersion(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>索引状态</span>
          <select value={state} onChange={(event) => setState(event.target.value)}>
            <option value="">全部状态</option>
            <option value="失败待重试">失败待重试</option>
            <option value="重试中">重试中</option>
            <option value="已完成">已完成</option>
            <option value="最终失败">最终失败</option>
            <option value="迟到归档">迟到归档</option>
          </select>
        </label>
        <label className="filter-item">
          <span>URL / 索引编号</span>
          <input value={keyword} placeholder="按URL或索引编号检索" onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <button className="btn" type="submit">查询</button>
        <button
          className="btn ghost"
          type="button"
          onClick={() => { setJob(''); setVersion(''); setState(''); setKeyword(''); setSelectedKey('') }}
        >
          重置条件
        </button>
      </form>

      <h3 className="section-title">作业执行进度</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>进度编号</th><th>作业编号</th><th>作业版本</th><th>进度状态</th>
            <th>总URL数</th><th>完成</th><th>失败</th><th>未收敛</th><th>最终失败</th>
            <th>迟到结果</th><th>版本冲突</th><th>核对</th><th />
          </tr>
        </thead>
        <tbody>
          {progressRows.map((row) => (
            <tr key={String(row.进度编号)} className={selectedKey === `${row.作业编号}|${row.作业版本}` ? 'row-selected' : undefined}>
              <td>{cell(row.进度编号)}</td>
              <td>{cell(row.作业编号)}</td>
              <td>{cell(row.作业版本)}</td>
              <td>{cell(row.进度状态 ?? row.status)}</td>
              <td>{cell(row.总URL数)}</td>
              <td>{cell(row.完成URL数)}</td>
              <td>{cell(row.失败URL数)}</td>
              <td>{cell(row.未收敛URL数)}</td>
              <td>{cell(row.最终失败URL数)}</td>
              <td>{cell(row.迟到结果数)}</td>
              <td>{row.冲突标记 ? '是' : '否'}</td>
              <td>{row.pending ? '执行中' : '已收敛'}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openAudit(row)}>查看明细</button>
              </td>
            </tr>
          ))}
          {progressRows.length === 0 && (
            <tr><td className="empty-state" colSpan={13}>暂无作业执行进度</td></tr>
          )}
        </tbody>
      </table>

      {audit && (
        <>
          <h3 className="section-title">
            URL集合核对 · {audit.作业编号} / v{audit.作业版本}（{audit.进度编号}）
          </h3>
          <div className="audit-summary">
            <span>URL集合大小：{audit.核对?.URL集合大小 ?? 0}</span>
            <span>总URL数：{audit.计数?.总URL数 ?? 0}</span>
            <span>完成 + 失败：{(audit.计数?.完成URL数 ?? 0) + (audit.计数?.失败URL数 ?? 0)}</span>
            <span>未收敛 + 最终失败 + 迟到归档：
              {(audit.计数?.未收敛URL数 ?? 0) + (audit.计数?.最终失败URL数 ?? 0) + (audit.计数?.迟到归档数 ?? 0)}
            </span>
            <strong className={audit.核对?.计数与URL集合一致 ? 'ok-text' : 'error-text'}>
              {audit.核对?.计数与URL集合一致 ? '计数与URL集合一致' : '计数与URL集合不一致'}
            </strong>
          </div>
          {audit.冲突标记 && (
            <p className="conflict-box">{audit.冲突处置}</p>
          )}
          <table className="data-table">
            <thead>
              <tr>
                <th>规范化URL</th><th>失败URL</th><th>失败原因码</th><th>结果状态</th>
                <th>索引编号</th><th>重试结果编号</th><th>重试结果</th><th>进度编号</th><th>迟到回写</th><th />
              </tr>
            </thead>
            <tbody>
              {audit.明细.map((detail) => (
                <tr key={detail.规范化URL}>
                  <td>{detail.规范化URL}</td>
                  <td>{detail.失败URL || '—'}</td>
                  <td>{detail.失败原因码 || '—'}</td>
                  <td>{detail.结果状态}</td>
                  <td>{detail.索引编号 || '—'}</td>
                  <td>{detail.重试结果编号 || '—'}</td>
                  <td>{detail.重试结果 || '—'}</td>
                  <td>{detail.进度编号 || '—'}</td>
                  <td>{detail.迟到回写 ? '是' : '否'}</td>
                  <td className="row-actions">
                    {detail.结果状态 !== '已完成' && (
                      <button
                        className="link"
                        type="button"
                        onClick={() => void reopen({
                          作业编号: detail.作业编号,
                          作业版本: detail.作业版本,
                          失败URL: detail.失败URL || detail.规范化URL,
                        })}
                      >
                        模拟重试成功
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {audit.明细.length === 0 && (
                <tr><td className="empty-state" colSpan={10}>该进度还没有URL记录</td></tr>
              )}
            </tbody>
          </table>

          <h3 className="section-title">版本依据（同一URL跨版本保留两组依据）</h3>
          <table className="data-table">
            <thead><tr><th>作业版本</th><th>来源</th><th>规范化URL</th><th>失败原因码</th></tr></thead>
            <tbody>
              {audit.版本依据.map((base, index) => (
                <tr key={`${base.作业版本}-${base.规范化URL}-${index}`}>
                  <td>{base.作业版本}</td>
                  <td>{base.来源}</td>
                  <td>{base.规范化URL}</td>
                  <td>{base.失败原因码 || '—'}</td>
                </tr>
              ))}
              {audit.版本依据.length === 0 && (
                <tr><td className="empty-state" colSpan={4}>暂无版本依据</td></tr>
              )}
            </tbody>
          </table>
        </>
      )}

      <h3 className="section-title">结果索引清单（重试回写落点）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>索引编号</th><th>作业编号</th><th>版本</th><th>进度编号</th><th>规范化URL</th>
            <th>结果状态</th><th>失败原因码</th><th>重试结果编号</th><th>迟到回写</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          {manifestRows.map((row) => (
            <tr key={String(row.索引编号)}>
              <td>{cell(row.索引编号)}</td>
              <td>{cell(row.作业编号)}</td>
              <td>{cell(row.作业版本)}</td>
              <td>{cell(row.进度编号)}</td>
              <td>{cell(row.规范化URL)}</td>
              <td>{cell(row.结果状态)}</td>
              <td>{cell(row.失败原因码)}</td>
              <td>{cell(row.重试结果编号)}</td>
              <td>{row.迟到回写 ? '是' : '否'}</td>
              <td>{cell(row.说明)}</td>
            </tr>
          ))}
          {manifestRows.length === 0 && (
            <tr><td className="empty-state" colSpan={10}>暂无索引清单记录</td></tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">重试结果明细</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>结果编号</th><th>作业编号</th><th>版本</th><th>规范化URL</th><th>尝试序号</th>
            <th>结果</th><th>失败原因码</th><th>迟到</th><th>幂等键</th><th>处置</th>
          </tr>
        </thead>
        <tbody>
          {resultRows.map((row) => (
            <tr key={String(row.结果编号)}>
              <td>{cell(row.结果编号)}</td>
              <td>{cell(row.作业编号)}</td>
              <td>{cell(row.作业版本)}</td>
              <td>{cell(row.规范化URL)}</td>
              <td>{cell(row.尝试序号)}</td>
              <td>{cell(row.结果)}</td>
              <td>{cell(row.失败原因码)}</td>
              <td>{row.迟到 ? '是' : '否'}</td>
              <td>{cell(row.幂等键)}</td>
              <td>{cell(row.处置)}</td>
            </tr>
          ))}
          {resultRows.length === 0 && (
            <tr><td className="empty-state" colSpan={10}>暂无重试结果</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>进度状态：{Object.values(stated).join(' / ')}</span>
        <span>
          {notice && <span className="ok-text">{notice}</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
