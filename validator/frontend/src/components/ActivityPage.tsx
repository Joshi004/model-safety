// A placeholder: the Activity page lists every conversion, scan and filter job once they can be started.
export default function ActivityPage() {
  return (
    <>
      <div className="page-header">
        <h1>Activity</h1>
      </div>
      <div className="card empty-state">
        <p>
          <strong>Nothing to show yet.</strong>
        </p>
        <p className="muted">
          Conversions, scans and filter jobs will be listed here, newest first, with their state and a Cancel
          button.
        </p>
      </div>
    </>
  )
}
