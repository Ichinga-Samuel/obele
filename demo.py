import random

from obele import Model, IntegerField, EmailField, TextField, Database, ForeignKeyField, DateTimeField


class User(Model):
    username = TextField()
    age = IntegerField(nullable=True)
    email = EmailField()


class Comment(Model):
    content = TextField(nullable=False)
    created = DateTimeField()
    author = ForeignKeyField(to=User, related_name='comments')


def create_random_users(count=10):
    random_users = [
        {   "id": random.randint(100, 1000),
            "username": f"user{i}",
            "age": random.randint(20, 60),
            "email": f"user{i}@localhost.com",
        }
        for i in range(count)
    ]
    random_users[0].pop("id")
    random_users[-1].pop('age')
    return User.bulk_create(random_users)


def create_users_posts():
    # create 10 random users and give them a random number of comments ranging from 0 to 6
    users = create_random_users()

    for user in users:
        num_comments = random.randint(0, 6)
        for _ in range(num_comments):
            Comment.create(
                content=f"Comment for {user.username}",
                # created=datetime.datetime.now(),
                author=user
            )

def read_user_comments():
    # read all users and print their comments
    for user in User.all():
        print(f"{user.username} ({user.email}):")
        for comment in user.comments:
            print(f"  - {comment.content} (created: {comment.created})")


def update_user():
    # update a user's email
    user = User.get(id=715)
    print(f"Before update: {user.age} ({user._pk_field})")
    user.age = 89
    user.save()
    user = User.get(id=715)
    print(f"after update: {user.age} ({user.email})")

def bulk_update():
    # Create multiple users and update their ages to current age * 2.
    User.create_table()
    users = User.bulk_create([
        {
            "username": f"bulk_user{i}",
            "age": random.randint(20, 60),
            "email": f"bulk_user{i}@localhost.com",
        }
        for i in range(5)
    ])

    print("Before bulk update:")
    for user in users:
        print(f"{user.id}: {user.username} age={user.age}")

    for user in users:
        user.age *= 2

    updated_count = User.bulk_update(users, fields=["age"])
    print(f"Updated {updated_count} user(s)")

    print("After bulk update:")
    updated_users = [User.get(id=user.id) for user in users]
    for user in updated_users:
        print(f"{user.id}: {user.username} age={user.age}")

    return updated_users


if __name__ == '__main__':
    with Database() as db:
        # User.create_table()
        # Comment.create_table()
        # create_users_posts()
        # print(Comment.get(id=1).select_related('author').first().author)
        # print(Comment.select_related('author').get(id=2).author)
        # print(User.get(id=15).comments.first().content)
        # print(len(User.get(id=16).comments.all()))
        # res = create_random_users(5)
        # print(res)
        bulk_update()
