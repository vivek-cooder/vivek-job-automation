from worker.tracker import (
    save_application,
    load_applications,
    already_applied,
    update_status
)


job = {
    "company": "Test Technologies",
    "job_title": "React Developer",
    "location": "Mumbai",
    "url": "https://example.com/job/123",
    "source": "Company Career Page"
}


print("Checking duplicate...")

if already_applied(
    job["company"],
    job["job_title"]
):
    print("Already applied. Skipping.")
else:

    save_application(
        company=job["company"],
        job_title=job["job_title"],
        location=job["location"],
        url=job["url"],
        source=job["source"],
        status="Waiting for user"
    )

    print("Application added to tracker.")


applications = load_applications()
print(f"\nTotal tracked applications: {len(applications)}")
for app in applications:
    print(f"- {app.get('company', 'Unknown')}: {app.get('job_title', 'Unknown')} [{app.get('status', 'Unknown')}]")